"""Unit tests for MCP tool binding in the agent factory and PTC integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from langchain_core.tools import tool, BaseTool

from k8s_autopilot.agent.factory import (
    _resolve_ptc_option,
    create_k8s_autopilot_agent,
)
from k8s_autopilot.mcp.session_manager import MCPSessionManager, _build_cached_mcp_tool


@pytest.mark.asyncio
async def test_cached_mcp_tool_execution() -> None:
    """Verify that calling a cached MCP tool invokes the session and formats output."""
    mock_mcp_tool = MagicMock()
    mock_mcp_tool.name = "query_logs"
    mock_mcp_tool.description = "Query logs"
    mock_mcp_tool.inputSchema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    from mcp.types import TextContent, CallToolResult

    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="log line 1\nlog line 2")],
            isError=False,
        )
    )

    manager = MCPSessionManager({"loki": {"transport": "http", "url": "http://localhost:3100"}})
    manager._sessions["loki"] = MagicMock(session=mock_session, exit_stack=AsyncMock())

    tool_obj = _build_cached_mcp_tool(
        mcp_tool=mock_mcp_tool,
        server_name="loki",
        session_manager=manager,
    )

    assert tool_obj.name == "loki:query_logs"
    assert tool_obj.description == "Query logs"

    result = await tool_obj.ainvoke({"query": "rate(http_requests[5m])"})
    mock_session.call_tool.assert_called_once_with("query_logs", {"query": "rate(http_requests[5m])"})
    assert "log line" in str(result)


def test_ptc_option_includes_mcp_tools() -> None:
    """Verify that _resolve_ptc_option correctly includes prefixed MCP tools in 'all' mode."""
    @tool
    def builtin_exec(cmd: str) -> str:
        """Execute command."""
        return cmd

    @tool
    def prometheus_query(query: str) -> str:
        """Query prometheus."""
        return query

    # Explicit name with prefix
    prometheus_query.name = "prometheus:query"

    tools = [builtin_exec, prometheus_query]
    ptc_tools = _resolve_ptc_option("all", tools=tools, acknowledge_unsafe=True, auto_approve=True)

    assert ptc_tools is not None
    assert "prometheus:query" in ptc_tools


@pytest.mark.asyncio
async def test_agent_factory_receives_mcp_tools(tmp_path) -> None:
    """Verify create_k8s_autopilot_agent accepts and registers mcp_tools."""
    @tool
    def mock_mcp_tool(arg: str) -> str:
        """Mock MCP tool."""
        return arg

    mock_mcp_tool.name = "prometheus:query"

    agent, backend = create_k8s_autopilot_agent(
        model="google_genai:gemini-3.7-flash",
        mcp_tools=[mock_mcp_tool],
        cwd=tmp_path,
        auto_approve=True,
    )

    assert agent is not None
    assert backend is not None
