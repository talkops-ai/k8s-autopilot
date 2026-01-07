"""
Helm Management Agent Module

This module provides the Helm Installation Management Agent for K8s Autopilot.
The agent manages the complete lifecycle of Helm chart installations following
a structured 5-phase workflow.

Key Features:
- Tools, Resources, and Prompts loaded from FastMCP Helm MCP Server
- Sub-agents (discovery, planner) handle specialized tasks
- HITL middleware ensures human approval for high-stakes operations
- Resource context injection for cluster-aware decisions

MCP Integration:
- Configured via k8s_autopilot.config.Config
- Uses standardized MCPAdapterClient

Architecture References:
- docs/deployment/helm-agent-architecture.md
- docs/deployment/fastmcp-server-architecture.md
"""

from k8s_autopilot.core.agents.helm_mgmt.helm_mgmt_agent import (
    # Main Agent
    k8sAutopilotHelmMgmtAgent,
    
    # Factory functions
    create_helm_mgmt_deep_agent,
    create_helm_mgmt_deep_agent_factory,
    
    # Middleware
    HelmAgentStateMiddleware,
    HelmApprovalHITLMiddleware,
    HelmApprovalHITLMiddleware,
    
    # HITL Tool
    request_human_input,
)

from k8s_autopilot.core.agents.helm_mgmt.helm_mgmt_prompts import (
    # Main prompts
    HELM_MGMT_SUPERVISOR_PROMPT,
    DISCOVERY_SUBAGENT_PROMPT,
    PLANNER_SUBAGENT_PROMPT,
    
    # Templates
    APPROVAL_TEMPLATES,
    ERROR_MESSAGES,
    STATUS_MESSAGES,
    
    # Reference content
    HELM_BEST_PRACTICES,
)

__all__ = [
    # Agent class
    "k8sAutopilotHelmMgmtAgent",
    
    # Factory functions
    "create_helm_mgmt_deep_agent",
    "create_helm_mgmt_deep_agent_factory",
    
    # Middleware
    "HelmAgentStateMiddleware",
    "HelmApprovalHITLMiddleware",
    "HelmApprovalHITLMiddleware",
    
    # HITL Tool
    "request_human_input",
    
    # Prompts
    "HELM_MGMT_SUPERVISOR_PROMPT",
    "DISCOVERY_SUBAGENT_PROMPT",
    "PLANNER_SUBAGENT_PROMPT",
    "APPROVAL_TEMPLATES",
    "ERROR_MESSAGES",
    "STATUS_MESSAGES",
    "HELM_BEST_PRACTICES",
]
