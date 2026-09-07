"""K8s and DevOps shell command safety classification and allow-list verification."""

from __future__ import annotations

import re
import shlex

# Characters and patterns that are disallowed in non-interactive allow-list mode
DANGEROUS_SHELL_PATTERNS: tuple[str, ...] = (
    "$(",  # Command substitution
    "`",  # Backtick command substitution
    "$'",  # ANSI-C quoting
    "\n",  # Newline
    "\r",  # Carriage return
    "\t",  # Tab
    "<(",  # Process substitution (input)
    ">(",  # Process substitution (output)
    "<<<",  # Here-string
    "<<",  # Here-doc
    ">>",  # Append redirect
    ">",  # Output redirect
    "<",  # Input redirect
    "${",  # Variable expansion with braces
)

# Commands that are ALWAYS safe (read-only, zero mutating side effects)
K8S_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        # Kubectl read-only
        "kubectl get",
        "kubectl describe",
        "kubectl explain",
        "kubectl api-resources",
        "kubectl api-versions",
        "kubectl top",
        "kubectl logs",
        "kubectl cluster-info",
        "kubectl config view",
        "kubectl config get-contexts",
        "kubectl config current-context",
        "kubectl version",
        "kubectl auth can-i",
        "kubectl diff",
        # Helm read-only
        "helm list",
        "helm status",
        "helm get",
        "helm show",
        "helm search",
        "helm env",
        "helm version",
        "helm repo list",
        "helm lint",
        "helm template",
        # ArgoCD read-only
        "argocd app get",
        "argocd app list",
        "argocd app diff",
        "argocd proj list",
        "argocd cluster list",
        "argocd version",
        # Generic safe exploration tools
        "git status",
        "git log",
        "git diff",
        "git branch",
        "git show",
        "cat",
        "ls",
        "find",
        "grep",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "diff",
        "jq",
        "yq",
        "echo",
        "pwd",
        "which",
    }
)

DEVOPS_SAFE_COMMANDS = list(K8S_SAFE_COMMANDS)

# Commands that are ALWAYS dangerous (require human approval or strict checks)
K8S_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"kubectl\s+delete\s+.*--all", re.IGNORECASE),
    re.compile(r"kubectl\s+delete\s+namespace\b", re.IGNORECASE),
    re.compile(r"kubectl\s+delete\s+ns\b", re.IGNORECASE),
    re.compile(r"kubectl\s+delete\s+all\b", re.IGNORECASE),
    re.compile(r"helm\s+uninstall\b", re.IGNORECASE),
    re.compile(r"helm\s+delete\b", re.IGNORECASE),
    re.compile(r"kubectl\s+drain\b", re.IGNORECASE),
    re.compile(r"kubectl\s+cordon\b", re.IGNORECASE),
    re.compile(r"kubectl\s+uncordon\b", re.IGNORECASE),
    re.compile(r"kubectl\s+taint\b", re.IGNORECASE),
    re.compile(r"argocd\s+app\s+delete\b", re.IGNORECASE),
    re.compile(r"kubectl\s+.*-n\s+kube-system\b", re.IGNORECASE),
    re.compile(r"kubectl\s+.*--namespace\s+kube-system\b", re.IGNORECASE),
    re.compile(r"kubectl\s+.*-n\s+kube-public\b", re.IGNORECASE),
    re.compile(r"kubectl\s+.*--namespace\s+kube-public\b", re.IGNORECASE),
    re.compile(r"kubectl\s+.*-n\s+kube-node-lease\b", re.IGNORECASE),
    re.compile(r"kubectl\s+.*--namespace\s+kube-node-lease\b", re.IGNORECASE),
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"mkfs", re.IGNORECASE),
    re.compile(r"dd\s+if=", re.IGNORECASE),
)

DEVOPS_DESTRUCTIVE_COMMANDS: list[str] = [
    "helm uninstall",
    "helm delete",
    "kubectl delete",
    "kubectl drain",
    "kubectl cordon",
    "kubectl taint",
    "argocd app delete",
]

# Production namespace patterns — mutating commands targeting these are classified dangerous
_PROD_NS_PATTERNS: re.Pattern[str] = re.compile(
    r"(?:-n|--namespace)[=\s]+[\"']?(?:prod|production|live|release)[a-zA-Z0-9_-]*[\"']?",
    re.IGNORECASE,
)


def classify_command(command: str) -> str:
    """Classify a shell command as 'safe', 'dangerous', or 'ambiguous'.

    - 'safe': Read-only queries with no state change
    - 'dangerous': High-risk operations (bulk delete, cluster drains, prod namespace changes, kube-system)
    - 'ambiguous': Standard mutations (apply, patch, rollout) that modify non-production resources
    """
    cmd = command.strip()
    if not cmd:
        return "safe"

    cmd_lower = cmd.lower()

    # 1. Check dangerous patterns first (e.g. bulk deletions, drains, kube-system)
    for pattern in K8S_DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return "dangerous"

    # 2. Check production namespace targeting
    # Even if safe read command, if dangerous check failed, mutating commands in prod are dangerous
    if _PROD_NS_PATTERNS.search(cmd) and not any(cmd_lower.startswith(safe) for safe in K8S_SAFE_COMMANDS):
        return "dangerous"

    # 3. Check safe command prefixes
    for safe in K8S_SAFE_COMMANDS:
        if cmd_lower.startswith(safe):
            return "safe"

    return "ambiguous"


def contains_dangerous_patterns(command: str) -> bool:
    """Return True if command contains shell injections, substitutions, or redirections."""
    if any(pattern in command for pattern in DANGEROUS_SHELL_PATTERNS):
        return True

    # Bare variable expansion
    if re.search(r"\$[A-Za-z_]", command):
        return True

    # Standalone & (background execution)
    return bool(re.search(r"(?<![&])&(?![&])", command))


def is_shell_command_allowed(command: str, allow_list: list[str] | None) -> bool:
    """Validate prefix match on whitespace-separated command tokens against an allow list."""
    if not allow_list or not command or not command.strip():
        return False

    if contains_dangerous_patterns(command):
        return False

    allow_entries = [entry.strip() for entry in allow_list if entry.strip()]
    if not allow_entries:
        return False

    # Split compound commands (&&, ||, ;, |)
    segments = re.split(r"&&|\|\||[|;]", command)
    found_command = False

    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment:
            continue

        try:
            tokens = shlex.split(segment)
            if not tokens:
                continue
            found_command = True

            matched = False
            for entry in allow_entries:
                entry_tokens = shlex.split(entry)
                if not entry_tokens:
                    continue
                if len(tokens) >= len(entry_tokens) and tokens[: len(entry_tokens)] == entry_tokens:
                    matched = True
                    break

            if not matched:
                return False
        except ValueError:
            return False

    return found_command
