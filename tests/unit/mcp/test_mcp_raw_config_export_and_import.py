"""Unit tests for raw JSON configuration export and import."""

from __future__ import annotations

from unittest.mock import AsyncMock
import pytest

from k8s_autopilot.mcp.raw_config import export_raw_mcp_config, import_raw_mcp_config


def test_export_raw_mcp_config_stdio_and_http() -> None:
    db_servers = [
        {
            "name": "prometheus-mcp-server",
            "transport": "http",
            "url": "http://0.0.0.0:8767/mcp",
            "command": None,
            "args": [],
            "env": {},
            "headers": {"Authorization": "Bearer token"},
            "disabled_tools": ["metric_drop"],
            "allowed_tools": [],
            "enabled": True,
            "trusted": True,
            "source": "project",
        },
        {
            "name": "helm-mcp-server",
            "transport": "stdio",
            "url": None,
            "command": "helm-mcp-server",
            "args": ["--port", "9000"],
            "env": {"MCP_ALLOW_WRITE": "true"},
            "headers": {},
            "disabled_tools": [],
            "allowed_tools": [],
            "enabled": False,
            "trusted": False,
            "source": "user",
        },
    ]

    exported = export_raw_mcp_config(db_servers)
    assert "mcpServers" in exported
    mcp_servers = exported["mcpServers"]

    # Remote server
    prom = mcp_servers["prometheus-mcp-server"]
    assert prom["serverUrl"] == "http://0.0.0.0:8767/mcp"
    assert prom["headers"] == {"Authorization": "Bearer token"}
    assert prom["disabledTools"] == ["metric_drop"]
    assert prom["disabled"] is False

    # Stdio server
    helm = mcp_servers["helm-mcp-server"]
    assert helm["command"] == "helm-mcp-server"
    assert helm["args"] == ["--port", "9000"]
    assert helm["env"] == {"MCP_ALLOW_WRITE": "true"}
    assert helm["disabled"] is True


@pytest.mark.asyncio
async def test_import_raw_mcp_config_upserts_to_store() -> None:
    raw_payload = {
        "mcpServers": {
            "tempo-mcp-server": {
                "serverUrl": "http://0.0.0.0:8769/mcp",
                "headers": {"X-Scope-OrgID": "tenant-1"},
                "disabled": False,
                "disabledTools": ["trace_delete"],
            },
            "copilotkit-mcp": {
                "command": "npx",
                "args": ["-y", "mcp-remote", "https://mcp.copilotkit.ai/sse"],
                "disabled": True,
            },
        }
    }

    mock_store = AsyncMock()
    mock_store.upsert_mcp_server = AsyncMock()

    synced = await import_raw_mcp_config(raw_payload, mock_store)
    assert "tempo-mcp-server" in synced
    assert "copilotkit-mcp" in synced
    assert mock_store.upsert_mcp_server.call_count == 2

    # Check first upsert
    call1_args = mock_store.upsert_mcp_server.call_args_list[0][0][0]
    assert call1_args["name"] == "tempo-mcp-server"
    assert call1_args["url"] == "http://0.0.0.0:8769/mcp"
    assert call1_args["enabled"] is True
    assert call1_args["disabled_tools"] == ["trace_delete"]
    assert call1_args["headers"] == {"X-Scope-OrgID": "tenant-1"}

    # Check second upsert
    call2_args = mock_store.upsert_mcp_server.call_args_list[1][0][0]
    assert call2_args["name"] == "copilotkit-mcp"
    assert call2_args["command"] == "npx"
    assert call2_args["args"] == ["-y", "mcp-remote", "https://mcp.copilotkit.ai/sse"]
    assert call2_args["enabled"] is False
