"""Raw JSON configuration import and export for MCP servers.

Supports standard Claude / VS Code `"mcpServers"` JSON specification.
"""

from __future__ import annotations

from typing import Any

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


def export_raw_mcp_config(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Export database records to standard Claude/VS Code `"mcpServers"` JSON format."""
    mcp_servers: dict[str, dict[str, Any]] = {}

    for srv in servers:
        name = srv.get("name")
        if not name:
            continue

        raw: dict[str, Any] = {}
        transport = srv.get("transport") or "stdio"
        url = srv.get("url")
        command = srv.get("command")

        if url or transport in ("http", "sse", "streamable_http"):
            raw["serverUrl"] = url or ""
            if srv.get("headers"):
                raw["headers"] = srv["headers"]
        elif command or transport == "stdio":
            raw["command"] = command or ""
            raw["args"] = srv.get("args", [])
            if srv.get("env"):
                raw["env"] = srv["env"]

        # Tool filtering
        if srv.get("disabled_tools"):
            raw["disabledTools"] = srv["disabled_tools"]
        elif "disabled_tools" in srv:
            raw["disabledTools"] = []

        if srv.get("allowed_tools"):
            raw["allowedTools"] = srv["allowed_tools"]

        # State toggle
        raw["disabled"] = not bool(srv.get("enabled", True))

        mcp_servers[name] = raw

    return {"mcpServers": mcp_servers}


async def import_raw_mcp_config(
    raw_config: dict[str, Any],
    store: Any,
    source: str = "user",
) -> list[str]:
    """Validate, normalize, and sync raw JSON configurations into the database."""
    if not isinstance(raw_config, dict):
        msg = f"Expected dict for raw MCP config, got {type(raw_config).__name__}"
        raise ValueError(msg)

    servers_dict = raw_config.get("mcpServers")
    if servers_dict is None:
        servers_dict = raw_config

    if not isinstance(servers_dict, dict):
        msg = f"Expected dict for 'mcpServers', got {type(servers_dict).__name__}"
        raise ValueError(msg)

    synced_servers: list[str] = []

    for name, config in servers_dict.items():
        if not isinstance(config, dict):
            continue

        # Extract url / serverUrl
        url = config.get("serverUrl") or config.get("url")
        command = config.get("command")

        # Resolve transport
        transport = config.get("transport") or config.get("type")
        if not transport:
            transport = ("sse" if "sse" in str(url).lower() else "http") if url else "stdio"

        # Enabled / disabled toggle
        if "disabled" in config:
            enabled = not bool(config["disabled"])
        elif "enabled" in config:
            enabled = bool(config["enabled"])
        else:
            enabled = True

        disabled_tools = config.get("disabledTools") or config.get("disabled_tools") or []
        allowed_tools = config.get("allowedTools") or config.get("allowed_tools") or []

        server_record = {
            "name": name,
            "transport": transport,
            "command": command,
            "args": config.get("args", []),
            "url": url,
            "env": config.get("env", {}),
            "headers": config.get("headers", {}),
            "source": config.get("source", source),
            "auth_token_env_var": config.get("auth_token_env_var"),
            "disabled_tools": disabled_tools if isinstance(disabled_tools, list) else [],
            "allowed_tools": allowed_tools if isinstance(allowed_tools, list) else [],
            "enabled": enabled,
            "trusted": bool(config.get("trusted", False)),
        }

        await store.upsert_mcp_server(server_record)
        synced_servers.append(name)

    return synced_servers
