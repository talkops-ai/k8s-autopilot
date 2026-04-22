"""Helm Operator Deep Agent Package.

Provides the HelmOperatorCoordinator that handles Helm chart generation
and updates as sub-agents within a single deep agent graph.

Extensibility:
    To add a new sub-agent domain, add its spec to ``subagents.py`` and
    optionally wrap it as a ``CompiledSubAgent`` for JIT MCP connections.

Reference: aws-orchestrator-agent tf_operator package
"""

from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator

__all__ = ["HelmOperatorCoordinator"]
