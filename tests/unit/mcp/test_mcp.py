"""Unit tests for MCP integration, discovery, arguments normalization, and Headless guard."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from k8s_autopilot.mcp.discovery import MCPDiscovery, discover_mcp_configs
from k8s_autopilot.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from k8s_autopilot.mcp.session_manager import (
    _is_transient_session_error,
    _normalize_mcp_arguments,
)
from k8s_autopilot.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
    mcp_tool_is_coherently_read_only,
)


def test_mcp_discovery_from_files(tmp_path: Path) -> None:
    """Verify MCP discovery loads and merges .mcp.json files."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test-server": {
                        "command": "test-cmd",
                        "args": ["--port", "8080"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    discovery = MCPDiscovery()
    configs = discovery.discover(project_root=tmp_path)
    assert "test-server" in configs
    assert configs["test-server"]["command"] == "test-cmd"
    assert configs["test-server"]["args"] == ["--port", "8080"]
    assert configs["test-server"]["source"] == "project"


def test_normalize_mcp_arguments() -> None:
    """Verify non-required empty strings are stripped from MCP arguments."""
    schema = {
        "type": "object",
        "required": ["mandatory_field"],
        "properties": {
            "mandatory_field": {"type": "string"},
            "optional_field": {"type": "string"},
            "optional_number": {"type": "number"},
        },
    }

    raw_args = {
        "mandatory_field": "",
        "optional_field": "",
        "optional_number": 42,
    }

    cleaned = _normalize_mcp_arguments(raw_args, schema)
    assert cleaned["mandatory_field"] == ""  # Required field preserved even if empty string
    assert "optional_field" not in cleaned  # Optional empty string stripped
    assert cleaned["optional_number"] == 42


def test_is_transient_session_error() -> None:
    """Verify socket/stream disconnections are recognized as transient."""
    assert _is_transient_session_error(BrokenPipeError()) is True
    assert _is_transient_session_error(ConnectionResetError()) is True
    assert _is_transient_session_error(ValueError("Bad value")) is False


def test_mcp_tool_read_only_annotations() -> None:
    """Verify MCP tool read-only metadata detection."""
    tool_ro = MagicMock()
    tool_ro.metadata = {"readOnlyHint": True, "destructiveHint": False}
    assert mcp_tool_is_coherently_read_only(tool_ro) is True

    tool_mut = MagicMock()
    tool_mut.metadata = {"readOnlyHint": False, "destructiveHint": True}
    assert mcp_tool_is_coherently_read_only(tool_mut) is False

    tool_no_meta = MagicMock()
    tool_no_meta.metadata = None
    assert mcp_tool_is_coherently_read_only(tool_no_meta) is False


def test_headless_mcp_guard_middleware() -> None:
    """Verify HeadlessMCPGuardMiddleware blocks mutating MCP tool calls."""
    guard = HeadlessMCPGuardMiddleware(tool_names=["helm_delete", "kubectl_delete"])

    # 1. Mutating tool call should be blocked with an error ToolMessage
    req_blocked = MagicMock()
    req_blocked.tool_call = {"id": "c-1", "name": "helm_delete", "args": {}}
    handler = MagicMock()

    result = guard.wrap_tool_call(req_blocked, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "headless runtime" in result.content
    handler.assert_not_called()

    # 2. Allowed read-only tool call should pass to handler
    req_allowed = MagicMock()
    req_allowed.tool_call = {"id": "c-2", "name": "kubectl_get", "args": {}}
    handler.return_value = ToolMessage(content="pods list", tool_call_id="c-2")

    result = guard.wrap_tool_call(req_allowed, handler)
    assert result.content == "pods list"
    handler.assert_called_once_with(req_allowed)


@pytest.mark.asyncio
async def test_cached_mcp_tool_execution_error_handling() -> None:
    """Verify that an MCP tool returning isError=True surfaces as an error block rather than crashing with ValueError."""
    from unittest.mock import AsyncMock
    import mcp.types as types
    from k8s_autopilot.mcp.session_manager import _build_cached_mcp_tool

    mock_mgr = MagicMock()
    mock_session = MagicMock()
    mock_mgr.get_session = AsyncMock(return_value=mock_session)

    # Tool returns isError=True
    mock_session.call_tool = AsyncMock(
        return_value=types.CallToolResult(
            content=[types.TextContent(type="text", text="release 'nginx' failed: not found")],
            isError=True,
        )
    )

    mcp_tool = types.Tool(
        name="helm_upgrade_release",
        description="Upgrade a helm release",
        inputSchema={"type": "object", "properties": {"release": {"type": "string"}}},
    )

    tool = _build_cached_mcp_tool(
        mcp_tool=mcp_tool,
        server_name="talkops-helm-mcp-server",
        session_manager=mock_mgr,
    )

    # Calling ainvoke should NOT raise ValueError("Since response_format='content_and_artifact'...")
    res = await tool.ainvoke({"release": "nginx"})
    assert isinstance(res, list)
    assert any("release 'nginx' failed" in str(block.get("text", "")) for block in res if isinstance(block, dict))

