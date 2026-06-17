"""
Sub-agent specifications for the K8s Operator Deep Agent coordinator.

Each sub-agent is a ``CompiledSubAgent`` with JIT MCP connection.
The K8s Operator currently provides one domain-specific subagent:
  - ``k8s-cluster-ops``  → kubernetes-mcp-server

Extensibility:
    To add another sub-agent:
    1. Define its prompt and dict spec below.
    2. Add a HITL middleware builder in ``middleware.py``.
    3. Add it to ``get_k8s_subagent_specs()``.
"""

from typing import Any, Callable, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("K8sSubagentFactory")

from k8s_autopilot.core.agents.k8s_operator.prompt_sections import (
    compose_subagent_prompt,
    create_subagent_registry,
)

# ---------------------------------------------------------------------------
# Composed subagent prompts (from prompt_sections.py)
#
# Each prompt is assembled from modular, testable blocks via PromptRegistry.
# Shared boilerplate (<scope>, <plan_locked_protocol>, <output_contract>,
# Iron Rules) is defined ONCE in prompt_sections.py and composed per-domain
# with domain-specific routing tables, safety rules, and workflow phases.
#
# To customise at runtime:
#     registry = create_subagent_registry("k8s-cluster-ops", safety_rules="<custom>")
#     custom_prompt = registry.compose()
# ---------------------------------------------------------------------------

K8S_CLUSTER_OPS_PROMPT = compose_subagent_prompt("k8s-cluster-ops")

# ---------------------------------------------------------------------------
# Sub-agent spec dict
# ---------------------------------------------------------------------------

K8S_CLUSTER_OPS_SUBAGENT: dict[str, Any] = {
    "name": "k8s-cluster-ops",
    "description": (
        "Specialized agent for Kubernetes cluster operations: listing/inspecting resources, "
        "pod logs and exec, applying YAML manifests, scaling deployments, deleting resources, "
        "running debug pods, viewing events, node diagnostics, cluster health checks, "
        "and kubeconfig context management. "
        "Connects directly to the kubernetes_mcp_server."
    ),
    "system_prompt": K8S_CLUSTER_OPS_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}


# ---------------------------------------------------------------------------
# JIT MCP Subagent Wrapper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The shared builder is imported from the central module to avoid 4x duplication.
# See shared_subagent.py for the full implementation (includes SkillsMiddleware).
from k8s_autopilot.core.agents.shared_subagent import build_mcp_subagent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_k8s_subagent_specs(
    coordinator_model: Any = None,
) -> list[Any]:
    """Assemble sub-agent specs for the K8s Operator deep agent.

    Returns the K8s Cluster Ops sub-agent as a JIT-connected MCP CompiledSubAgent.
    """
    from k8s_autopilot.core.agents.k8s_operator.middleware import (
        build_k8s_cluster_ops_hitl_middleware,
    )
    from k8s_autopilot.core.agents.shared_middleware import (
        make_subagent_interpreter_builder,
        K8S_CLUSTER_OPS_PTC_ALLOWLIST,
    )

    coord_model = coordinator_model or ""

    return [
        # Kubernetes Cluster Operations sub-agent
        build_mcp_subagent(
            K8S_CLUSTER_OPS_SUBAGENT,
            server_filter=["kubernetes_mcp_server"],
            mcp_resource_server_name="kubernetes_mcp_server",
            include_filesystem=True,
            skill_paths=["/skills/k8s-operator/"],
            hitl_builder=build_k8s_cluster_ops_hitl_middleware,
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=K8S_CLUSTER_OPS_PTC_ALLOWLIST,
                ),
            ],
        ),
    ]
