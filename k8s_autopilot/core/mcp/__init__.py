"""
Dynamic MCP Server Discovery, Domain Binding & Session Management.

Provides project-level ``.mcp.json`` discovery, trust-gated loading,
domain-scoped server binding, and startup session management for
k8s-autopilot deep agents.
"""

from k8s_autopilot.core.mcp.discovery import MCPDiscovery, MCPServerSpec
from k8s_autopilot.core.mcp.trust import MCPTrustStore
from k8s_autopilot.core.mcp.session_manager import MCPSessionManager, MCPServerResult

__all__ = [
    "MCPDiscovery",
    "MCPServerSpec",
    "MCPTrustStore",
    "MCPSessionManager",
    "MCPServerResult",
]
