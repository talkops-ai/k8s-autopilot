"""CLI AST Safety Evaluator — deterministic safety classification for shell commands.

Parses composite bash commands, subshells, pipelines, and process substitutions using
`bashlex` AST traversal to detect dangerous verbs and flags in sub-millisecond time.
"""

from __future__ import annotations

from typing import Any

import bashlex
import bashlex.ast

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

# Wrapper utilities that execute other commands
WRAPPER_UTILITIES: frozenset[str] = frozenset(
    {
        "xargs",
        "sudo",
        "doas",
        "time",
        "nohup",
        "env",
        "parallel",
        "chroot",
        "exec",
        "sh",
        "bash",
        "zsh",
    }
)

# Pure read-only utilities whose execution only inspects local data/state
READONLY_UTILITIES: frozenset[str] = frozenset(
    {
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "head",
        "tail",
        "ls",
        "find",
        "echo",
        "pwd",
        "awk",
        "sed",
        "jq",
        "yq",
        "wc",
        "diff",
        "stat",
        "file",
        "uname",
        "which",
        "whereis",
        "whoami",
        "printenv",
        "date",
        "uptime",
        "df",
        "du",
        "free",
        "ps",
        "top",
        "tree",
        "sort",
        "uniq",
        "tr",
        "cut",
        "less",
        "more",
        "curl",
    }
)

# Destructive operations that delete or disrupt live cluster state, disk, or databases
DANGEROUS_VERBS: frozenset[str] = frozenset(
    {
        "delete",
        "remove",
        "rm",
        "drain",
        "evict",
        "taint",
        "drop",
        "uninstall",
        "zap",
        "destroy",
        "prune",
        "truncate",
        "kill",
        "purge",
        "wipe",
        "format",
        "cordon",
        "uncordon",
        "shutdown",
        "reboot",
    }
)

# Destructive force-override CLI flags
DANGEROUS_FLAGS: frozenset[str] = frozenset(
    {
        "--force",
        "--purge",
        "--grace-period=0",
        "-rf",
        "-fr",
        "--all",
        "--no-preserve-root",
        "--cascade=foreground",
        "--cascade=orphan",
        "--hard",
        "-D",
    }
)

# Standard Kubernetes/Helm/Git read-only verbs
READONLY_VERBS: frozenset[str] = frozenset(
    {
        "get",
        "list",
        "describe",
        "status",
        "logs",
        "log",
        "version",
        "show",
        "view",
        "explain",
        "top",
        "diff",
        "cluster-info",
        "auth",
        "can-i",
        "api-resources",
        "api-versions",
        "branch",
        "remote",
        "config",
    }
)


class SecurityASTVisitor(bashlex.ast.nodevisitor):
    """Traverses bashlex AST nodes inspecting words, commands, and substitutions."""

    def __init__(self) -> None:
        """Initialize SecurityASTVisitor with empty collections and default safety flags."""
        self.found_verbs: list[str] = []
        self.found_flags: list[str] = []
        self.found_commands: list[str] = []
        self.is_destructive: bool = False
        self.is_readonly: bool = True

    def visitcommand(self, n: Any, parts: list[Any]) -> None:
        """Called for every command node in the AST."""
        words: list[str] = []
        for part in parts:
            if getattr(part, "kind", None) == "word":
                w = getattr(part, "word", "")
                if w:
                    words.append(w)

        if not words:
            return

        cmd_utility = words[0].lower()
        self.found_commands.append(cmd_utility)

        # 1. Pure inspection utilities (cat, grep, ls, jq, etc.)
        if cmd_utility in READONLY_UTILITIES:
            if cmd_utility == "rm":
                self.is_destructive = True
                self.is_readonly = False
            return

        # 2. Wrapper utilities (xargs, sudo, time, etc.) - inspect all words
        if cmd_utility in WRAPPER_UTILITIES:
            self.is_readonly = False
            for word in words[1:]:
                w_lower = word.lower()
                if word.startswith("-"):
                    self.found_flags.append(word)
                    if word in DANGEROUS_FLAGS or w_lower in DANGEROUS_FLAGS:
                        self.is_destructive = True
                else:
                    self.found_verbs.append(w_lower)
                    if w_lower in DANGEROUS_VERBS:
                        self.is_destructive = True
            return

        # 3. Command utilities with sub-commands (kubectl, helm, git, docker, podman)
        flags: list[str] = []
        non_flag_args: list[str] = []

        for word in words[1:]:
            w_lower = word.lower()
            if word.startswith("-"):
                flags.append(word)
                if (
                    word in DANGEROUS_FLAGS
                    or w_lower in DANGEROUS_FLAGS
                    or (cmd_utility == "rm" and ("f" in word or "r" in word))
                ):
                    self.is_destructive = True
                    self.is_readonly = False
            else:
                non_flag_args.append(w_lower)

        self.found_flags.extend(flags)

        if not non_flag_args:
            return

        # The primary verb is the first non-flag argument (e.g. kubectl get, helm install)
        primary_verb = non_flag_args[0]
        self.found_verbs.append(primary_verb)

        if primary_verb in DANGEROUS_VERBS:
            self.is_destructive = True
            self.is_readonly = False
        elif primary_verb in READONLY_VERBS:
            # Known read-only verb for kubectl, helm, git
            pass
        else:
            # Mutating action (e.g. apply, patch, scale, rollout, install, upgrade)
            self.is_readonly = False

    def visitprocesssubstitution(self, n: Any, command: Any) -> None:
        """Traverse into process substitutions <(...) or >(...)."""
        self.visit(command)

    def visitcommandsubstitution(self, n: Any, command: Any) -> None:
        """Traverse into command substitutions $(...) or `...`."""
        self.visit(command)


def evaluate_cli_safety(command_string: str) -> dict[str, Any]:
    """Parse a composite shell command and determine its operational risk tier.

    Returns:
        dict containing:
            - tier: 1 (Read-Only), 2 (Low-Impact), 3 (Mutating), or 4 (Destructive)
            - is_destructive: bool
            - is_readonly: bool
            - safe: bool
            - reason: str
    """
    cmd_str = (command_string or "").strip()
    if not cmd_str:
        return {
            "tier": 1,
            "is_destructive": False,
            "is_readonly": True,
            "safe": True,
            "reason": "Empty command string.",
        }

    try:
        trees = bashlex.parse(cmd_str)
    except Exception as exc:
        # If the command syntax cannot be safely parsed, fail-closed to Tier 4
        logger.warning("AST Parsing failed for command %r: %s", cmd_str[:120], exc)
        return {
            "tier": 4,
            "is_destructive": True,
            "is_readonly": False,
            "safe": False,
            "reason": f"AST parsing failed (fail-closed security): {exc}",
        }

    visitor = SecurityASTVisitor()
    for tree in trees:
        visitor.visit(tree)

    if visitor.is_destructive:
        matched_verbs = set(visitor.found_verbs) & DANGEROUS_VERBS
        matched_flags = set(visitor.found_flags) & DANGEROUS_FLAGS
        reasons: list[str] = []
        if matched_verbs:
            reasons.append(f"destructive verbs: {sorted(matched_verbs)}")
        if matched_flags:
            reasons.append(f"destructive flags: {sorted(matched_flags)}")
        if not reasons:
            reasons.append("destructive command pattern")
        reason_str = f"Detected {', '.join(reasons)}."
        res = {
            "tier": 4,
            "is_destructive": True,
            "is_readonly": False,
            "safe": False,
            "reason": reason_str,
        }
    elif visitor.is_readonly:
        res = {
            "tier": 1,
            "is_destructive": False,
            "is_readonly": True,
            "safe": True,
            "reason": "Command contains only read-only inspection utilities and verbs.",
        }
    else:
        # Non-destructive mutation (e.g. apply, patch, rollout, label) -> Tier 3
        res = {
            "tier": 3,
            "is_destructive": False,
            "is_readonly": False,
            "safe": False,
            "reason": f"Operational mutation command: verbs {visitor.found_verbs[:3]}.",
        }

    logger.debug(
        "Evaluated shell command: is_safe=%s, mutated=%s",
        res["safe"],
        not res["is_readonly"],
        extra={
            "command": cmd_str[:120],
            "is_safe": res["safe"],
            "detected_actions": list(visitor.found_verbs),
        },
    )
    return res
