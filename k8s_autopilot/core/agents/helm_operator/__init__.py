"""Helm Operator Deep Agent Package.

Provides the HelmOperatorCoordinator that handles Helm chart generation,
updates, and live cluster operations as sub-agents within a single deep agent graph.

Architecture (dcode-aligned):
    - Subagents discovered from ``memory/helm-operator/agents/*/AGENTS.md``
    - MCP tools resolved at startup via ``MCPSessionManager``
    - Frontmatter-driven middleware assembly (HITL, PTC, extra tools)

To add a new sub-agent:
    1. Create ``memory/helm-operator/agents/{name}/AGENTS.md`` with YAML frontmatter
    2. The coordinator discovers it automatically on next startup

Reference: dcode/code/agent.py, aws-orchestrator-agent tf_operator package
"""

from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator

__all__ = ["HelmOperatorCoordinator"]
