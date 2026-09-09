"""Model Context Protocol (MCP) integration module for K8s Autopilot."""

from k8s_autopilot.mcp.config import resolve_mcp_server_env
from k8s_autopilot.mcp.discovery import (
    MCPDiscovery,
    MCPServerConfig,
    discover_mcp_configs,
)
from k8s_autopilot.mcp.mcp_info import (
    MCPServerInfo,
    MCPServerStatus,
    MCPToolInfo,
)
from k8s_autopilot.mcp.preload import (
    format_mcp_status_response,
    preload_mcp_metadata,
    probe_one_mcp_server,
)
from k8s_autopilot.mcp.raw_config import (
    export_raw_mcp_config,
    import_raw_mcp_config,
)
from k8s_autopilot.mcp.session_manager import (
    MCPSessionManager,
    _is_transient_session_error,
    _normalize_mcp_arguments,
)
from k8s_autopilot.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
    mcp_tool_is_coherently_read_only,
)

__all__ = [
    "HeadlessMCPGuardMiddleware",
    "MCPDiscovery",
    "MCPServerConfig",
    "MCPServerInfo",
    "MCPServerStatus",
    "MCPSessionManager",
    "MCPToolInfo",
    "_is_transient_session_error",
    "_normalize_mcp_arguments",
    "discover_mcp_configs",
    "export_raw_mcp_config",
    "format_mcp_status_response",
    "gated_mcp_tool_names",
    "import_raw_mcp_config",
    "mcp_tool_is_coherently_read_only",
    "preload_mcp_metadata",
    "probe_one_mcp_server",
    "resolve_mcp_server_env",
]
