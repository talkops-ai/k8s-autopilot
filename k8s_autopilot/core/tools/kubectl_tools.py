"""
Generic kubectl read-only tool for deep agent subagents.

Provides a factory function ``create_kubectl_readonly_tool()`` that returns a
``StructuredTool`` executing read-only kubectl commands via ``subprocess``.

**Design: Blocked-patterns approach** — instead of a fixed allowlist of verbs,
the tool blocks known mutating verbs and flags.  Any kubectl command that does
not match a blocked pattern is permitted.  This covers ``get``, ``describe``,
``logs``, ``top``, ``api-resources``, ``explain``, ``diff``, ``version``,
``cluster-info``, and future read-only subcommands automatically.

Safety characteristics:
    - ``subprocess.run(shell=False)`` — no shell injection
    - Configurable timeout (default 30 s)
    - Output truncation (default 50 KB)
    - Command must start with ``kubectl``

Usage::

    from k8s_autopilot.core.tools.kubectl_tools import create_kubectl_readonly_tool

    # In any subagent builder:
    build_mcp_subagent(
        MY_SUBAGENT,
        server_filter=["my_mcp_server"],
        mcp_resource_server_name="my_mcp_server",
        extra_tools=[create_kubectl_readonly_tool()],
    )
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any, FrozenSet, Optional

from langchain_core.tools import StructuredTool

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("KubectlReadonly")


# ---------------------------------------------------------------------------
# Blocked patterns — mutating / dangerous kubectl verbs & flags
# ---------------------------------------------------------------------------
# These are verbs (the first positional arg after ``kubectl``) that perform
# state-changing operations.  Any command whose first positional arg matches
# one of these is rejected.  Multi-word verbs like ``config set`` are matched
# by checking two consecutive tokens.

_BLOCKED_VERBS: FrozenSet[str] = frozenset({
    # Resource mutation
    "apply",
    "create",
    "delete",
    "edit",
    "patch",
    "replace",
    # Scaling / rollout
    "scale",
    "autoscale",
    "rollout",
    "set",
    # Node management
    "cordon",
    "uncordon",
    "drain",
    "taint",
    # Metadata mutation
    "label",
    "annotate",
    # Interactive / exec
    "run",
    "exec",
    "attach",
    "cp",
    # Network
    "port-forward",
    "proxy",
    # Auth / cert
    "certificate",
    "auth",
    # Debug (can create ephemeral containers)
    "debug",
})

# Multi-word blocked verbs — checked as consecutive token pairs.
# e.g. ``kubectl config set-context ...`` is blocked but
# ``kubectl config view`` is allowed.
_BLOCKED_MULTI_VERBS: FrozenSet[str] = frozenset({
    "config set",
    "config set-context",
    "config set-cluster",
    "config set-credentials",
    "config delete-context",
    "config delete-cluster",
    "config rename-context",
    "config use-context",
    "config unset",
})

# Flags that are dangerous even on normally-read-only commands.
_BLOCKED_FLAGS: FrozenSet[str] = frozenset({
    "--force",
    "--grace-period=0",
})

# Default limits
_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_OUTPUT = 51200  # 50 KB


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def _validate_command(tokens: list[str]) -> Optional[str]:
    """Validate a tokenized kubectl command against blocked patterns.

    Returns an error message if the command is blocked, or ``None`` if allowed.
    """
    if not tokens:
        return "BLOCKED: empty command"

    if tokens[0] != "kubectl":
        return (
            "BLOCKED: command must start with 'kubectl'. "
            "Got: '{}'".format(tokens[0])
        )

    if len(tokens) < 2:
        return "BLOCKED: no kubectl subcommand provided"

    # Strip global flags that appear before the verb
    # (e.g. ``kubectl --context=prod get pods``)
    verb_idx = 1
    while verb_idx < len(tokens) and tokens[verb_idx].startswith("-"):
        verb_idx += 1

    if verb_idx >= len(tokens):
        return "BLOCKED: no kubectl subcommand found after flags"

    verb = tokens[verb_idx].lower()

    # Check single-word blocked verbs
    if verb in _BLOCKED_VERBS:
        return (
            f"BLOCKED: kubectl verb '{verb}' is a mutating operation and is "
            f"not permitted.  Only read-only commands are allowed."
        )

    # Check multi-word blocked verbs (e.g. ``config set-context``)
    if verb_idx + 1 < len(tokens):
        multi_verb = f"{verb} {tokens[verb_idx + 1].lower()}"
        if multi_verb in _BLOCKED_MULTI_VERBS:
            return (
                f"BLOCKED: kubectl '{multi_verb}' is a mutating operation "
                f"and is not permitted."
            )

    # Check blocked flags anywhere in the command
    for token in tokens[verb_idx:]:
        token_lower = token.lower()
        if token_lower in _BLOCKED_FLAGS:
            return (
                f"BLOCKED: flag '{token}' is not permitted in read-only mode."
            )

    return None  # Command is allowed


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_kubectl_readonly_tool(
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT,
) -> StructuredTool:
    """Create a kubectl read-only diagnostic tool.

    Generic factory — any subagent builder can call this to get a kubectl
    diagnostic tool.  Uses blocked-patterns approach: all read operations
    are allowed; only known mutating verbs/flags are rejected.

    Args:
        timeout: Maximum execution time in seconds (default 30).
        max_output_bytes: Maximum stdout/stderr size before truncation
            (default 50 KB).

    Returns:
        A ``StructuredTool`` suitable for passing to ``extra_tools=``
        in ``build_mcp_subagent()``.
    """

    def kubectl_readonly(command: str) -> str:
        """Execute a read-only kubectl command for cluster diagnostics.

        Use this tool to inspect Kubernetes resources after Helm operations.
        All read operations (get, describe, logs, top, events, etc.) are allowed.
        Mutating operations (apply, create, delete, exec, scale, etc.) are BLOCKED.

        Args:
            command: The full kubectl command string.
                Examples:
                    kubectl get pods -n my-namespace
                    kubectl describe pod my-pod -n my-namespace
                    kubectl logs my-pod -n my-namespace --tail=100
                    kubectl get events -n my-namespace --sort-by='.lastTimestamp'
                    kubectl get deploy -A
                    kubectl top pods -n my-namespace

        Returns:
            JSON string with stdout, stderr, exit_code, the command that was
            executed, and a truncated flag.
        """
        try:
            tokens = shlex.split(command.strip())
        except ValueError as e:
            return json.dumps({
                "error": f"BLOCKED: failed to parse command: {e}",
                "command": command,
            })

        # Validate against blocked patterns
        block_reason = _validate_command(tokens)
        if block_reason:
            logger.warning(
                "kubectl_readonly: command blocked",
                extra={"command": command, "reason": block_reason},
            )
            return json.dumps({
                "error": block_reason,
                "command": command,
            })

        # Execute
        logger.info(
            "kubectl_readonly: executing",
            extra={"command": command},
        )

        try:
            result = subprocess.run(
                tokens,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,  # Explicit: no shell injection
            )

            stdout = result.stdout or ""
            stderr = result.stderr or ""
            truncated = False

            # Truncate if output is too large
            if len(stdout.encode("utf-8", errors="replace")) > max_output_bytes:
                stdout = stdout[:max_output_bytes] + "\n... [OUTPUT TRUNCATED]"
                truncated = True

            if len(stderr.encode("utf-8", errors="replace")) > max_output_bytes:
                stderr = stderr[:max_output_bytes] + "\n... [OUTPUT TRUNCATED]"
                truncated = True

            return json.dumps({
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": result.returncode,
                "command": command,
                "truncated": truncated,
            })

        except subprocess.TimeoutExpired:
            logger.warning(
                "kubectl_readonly: command timed out",
                extra={"command": command, "timeout": timeout},
            )
            return json.dumps({
                "error": f"Command timed out after {timeout} seconds",
                "command": command,
            })
        except FileNotFoundError:
            return json.dumps({
                "error": (
                    "kubectl binary not found.  Ensure kubectl is installed "
                    "and available on PATH."
                ),
                "command": command,
            })
        except Exception as e:
            logger.error(
                "kubectl_readonly: unexpected error",
                extra={"command": command, "error": str(e)},
            )
            return json.dumps({
                "error": f"Unexpected error: {e}",
                "command": command,
            })

    return StructuredTool.from_function(
        func=kubectl_readonly,
        name="kubectl_readonly",
        description=(
            "Execute a read-only kubectl command for cluster diagnostics. "
            "Use to inspect pods, deployments, services, events, logs, etc. "
            "after Helm operations.  All read operations are allowed; "
            "mutating operations (apply, create, delete, exec, scale) are "
            "blocked.  Returns JSON with stdout, stderr, and exit_code."
        ),
    )
