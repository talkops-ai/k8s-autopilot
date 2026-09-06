"""Unit tests for MCP server health probing and diagnostics engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from k8s_autopilot.mcp.preload import (
    format_mcp_status_response,
    preload_mcp_metadata,
    probe_one_mcp_server,
)


@pytest.mark.asyncio
async def test_probe_disabled_server_no_network() -> None:
    """Verify that a disabled server returns status='disabled' immediately."""
    config = {
        "transport": "http",
        "url": "http://127.0.0.1:9999/mcp",
        "enabled": False,
    }

    info = await probe_one_mcp_server("disabled-srv", config)
    assert info.name == "disabled-srv"
    assert info.status == "disabled"
    assert info.enabled is False
    assert info.tool_count == 0
    assert info.tools == ()


@pytest.mark.asyncio
async def test_probe_offline_server_error_isolation() -> None:
    """Verify that probing an unreachable endpoint captures error without crashing."""
    config = {
        "transport": "http",
        "url": "http://127.0.0.1:59999/nonexistent",
        "enabled": True,
    }

    info = await probe_one_mcp_server("offline-srv", config)
    assert info.name == "offline-srv"
    assert info.status == "error"
    assert info.error is not None
    assert info.tool_count == 0


@pytest.mark.asyncio
async def test_probe_active_server_success() -> None:
    """Verify that probing an active server discovers and prefixes tools."""
    mock_tool1 = MagicMock()
    mock_tool1.name = "query_metric"
    mock_tool1.description = "Query prometheus metrics"
    mock_tool1.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}

    mock_tool2 = MagicMock()
    mock_tool2.name = "list_alerts"
    mock_tool2.description = "List active alerts"
    mock_tool2.inputSchema = {"type": "object"}

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[mock_tool1, mock_tool2]))

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("langchain_mcp_adapters.sessions.create_session", return_value=mock_ctx):
        config = {
            "transport": "http",
            "url": "http://mock-mcp:8080/mcp",
            "enabled": True,
        }
        info = await probe_one_mcp_server("prometheus", config)

        assert info.name == "prometheus"
        assert info.status == "ok"
        assert info.error is None
        assert info.tool_count == 2
        assert info.tools[0].name == "prometheus:query_metric"
        assert info.tools[0].description == "Query prometheus metrics"
        assert info.tools[1].name == "prometheus:list_alerts"


@pytest.mark.asyncio
async def test_probe_tool_filtering() -> None:
    """Verify that disabled_tools and allowed_tools filter discovered tools during probe."""
    mock_t1 = MagicMock()
    mock_t1.name = "get_logs"
    mock_t1.description = "Get logs"
    mock_t1.inputSchema = {}

    mock_t2 = MagicMock()
    mock_t2.name = "delete_logs"
    mock_t2.description = "Delete logs"
    mock_t2.inputSchema = {}

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[mock_t1, mock_t2]))

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("langchain_mcp_adapters.sessions.create_session", return_value=mock_ctx):
        config = {
            "transport": "http",
            "url": "http://mock-mcp:8080/mcp",
            "enabled": True,
            "disabled_tools": ["delete_*"],
        }
        info = await probe_one_mcp_server("loki", config)

        assert info.tool_count == 1
        assert info.tools[0].name == "loki:get_logs"


@pytest.mark.asyncio
async def test_probe_unauthenticated_status() -> None:
    """Verify that 401/OAuth challenge errors are classified as 'unauthenticated'."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("HTTP 401 Unauthorized: Bearer OAuth challenge"))
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("langchain_mcp_adapters.sessions.create_session", return_value=mock_ctx):
        config = {
            "transport": "http",
            "url": "https://api.github.com/mcp",
            "enabled": True,
        }
        info = await probe_one_mcp_server("github", config)
        assert info.status == "unauthenticated"


@pytest.mark.asyncio
async def test_preload_mcp_metadata_and_format_response() -> None:
    """Verify concurrent preload and status response formatting."""
    configs = {
        "server-a": {"enabled": False, "transport": "http"},
        "server-b": {"enabled": False, "transport": "stdio", "command": "cmd"},
    }

    infos = await preload_mcp_metadata(configs)
    assert len(infos) == 2
    assert all(i.status == "disabled" for i in infos)

    payload = format_mcp_status_response(infos)
    assert "total_tools" in payload
    assert payload["total_tools"] == 0
    assert len(payload["servers"]) == 2
    assert payload["servers"][0]["name"] == "server-a"
    assert payload["servers"][0]["status"] == "disabled"
