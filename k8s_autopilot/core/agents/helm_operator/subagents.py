"""
Sub-agent specifications for the Helm Operator Deep Agent coordinator.

**MIGRATION NOTE (dcode v2)**:
    All hardcoded subagent prompts and dict specs have been migrated to
    filesystem ``AGENTS.md`` files under ``memory/helm-operator/agents/``.
    The coordinator now uses ``SubagentRegistry.list_subagents()`` to
    discover them dynamically and ``build_dynamic_subagent_spec()`` to
    assemble dict specs with pre-resolved MCP tools.

    This module is retained as a **compatibility shim** for any code that
    still imports from it.  New code should import from:
    - ``k8s_autopilot.core.agents.registry`` (SubagentRegistry)
    - ``k8s_autopilot.core.agents.helm_operator.middleware`` (build_dynamic_subagent_spec)

Filesystem subagent locations::

    memory/helm-operator/agents/helm-generator/AGENTS.md
    memory/helm-operator/agents/helm-validator/AGENTS.md
    memory/helm-operator/agents/helm-updater/AGENTS.md
    memory/helm-operator/agents/helm-operation/AGENTS.md
    memory/helm-operator/agents/github-agent/AGENTS.md

Reference: aws-orchestrator-agent tf_operator/subagents.py (legacy)
"""

from __future__ import annotations

import warnings
from typing import Any, List

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("HelmSubagentFactory")


# ---------------------------------------------------------------------------
# Helm-specific resource description (kept here for backward compatibility)
# ---------------------------------------------------------------------------

HELM_RESOURCE_DESCRIPTION = (
    "Read content of a specific MCP resource by URI "
    "(server: helm_mcp_server). Use this to read "
    "helm releases, chart metadata, and cluster state natively.\n\n"
    "STRICT URI FORMAT RULES:\n"
    "You MUST use exactly one of these formats. DO NOT append `/values`, `?namespace=`, or guess URIs.\n"
    "- `helm://releases`\n"
    "- `helm://releases/[release_name]` (WARNING: namespace filtering is NOT supported. NEVER put namespace in URI)\n"
    "- `helm://charts`\n"
    "- `helm://charts/[repo]/[name]`\n"
    "- `helm://charts/[repo]/[name]/readme`\n"
    "- `kubernetes://cluster-info`\n"
    "- `kubernetes://namespaces`\n"
    "- `helm://best_practices`"
)


# ---------------------------------------------------------------------------
# Deprecated public API — compatibility shim
# ---------------------------------------------------------------------------

def get_helm_subagent_specs(
    coordinator_model: Any = None,
    validator_model: Any = None,
) -> List[Any]:
    """**DEPRECATED**: Use ``SubagentRegistry.list_subagents()`` + ``build_dynamic_subagent_spec()`` instead.

    This function is retained for backward compatibility only.  The coordinator
    no longer calls it — subagent discovery is now filesystem-driven.

    Raises:
        DeprecationWarning: Always emitted on call.

    Returns:
        Empty list.  Callers should migrate to the new dynamic pattern.
    """
    warnings.warn(
        "get_helm_subagent_specs() is deprecated. "
        "Use SubagentRegistry.list_subagents() + build_dynamic_subagent_spec() instead. "
        "Subagent specs are now discovered from memory/helm-operator/agents/*/AGENTS.md.",
        DeprecationWarning,
        stacklevel=2,
    )
    _subagent_logger.warning(
        "get_helm_subagent_specs() called — this is a deprecated compatibility shim. "
        "Migrate to SubagentRegistry + build_dynamic_subagent_spec()."
    )
    return []
