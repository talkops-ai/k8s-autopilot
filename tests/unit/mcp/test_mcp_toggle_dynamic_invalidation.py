"""Unit tests for dynamic MCP server toggle, session eviction, cache invalidation, and executor graph rebuilding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.api.settings_routes import create_settings_routes, set_config_store
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.mcp.preload import (
    MCPServerInfo,
    MCPToolInfo,
    get_cached_mcp_server_infos,
    set_cached_mcp_server_info,
    clear_cached_mcp_server_infos,
)
from k8s_autopilot.mcp.session_manager import MCPSessionManager
from k8s_autopilot.server.executor import A2AAutoPilotExecutor


@pytest.fixture
async def mcp_test_env(tmp_path: Path):
    clear_cached_mcp_server_infos()
    adapter = SqliteConfigAdapter(tmp_path / "test_mcp_dynamic.db")
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    routes = create_settings_routes()
    app = Starlette(routes=routes)
    client = TestClient(app)

    yield store, client
    clear_cached_mcp_server_infos()


@pytest.mark.asyncio
async def test_toggle_mcp_server_disables_and_evicts_session(mcp_test_env) -> None:
    store, client = mcp_test_env

    # 1. Seed active github server in store
    github_server = {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "enabled": True,
        "source": "user",
    }
    await store.upsert_mcp_server(github_server)

    # 2. Setup mock live session in MCPSessionManager
    mock_exit_stack = AsyncMock()
    mock_session = AsyncMock()
    mcp_mgr = MCPSessionManager.get_instance({"github": github_server})
    session_entry = MagicMock()
    session_entry.session = mock_session
    session_entry.exit_stack = mock_exit_stack
    mcp_mgr._sessions["github"] = session_entry

    # Seed active info in probed cache
    set_cached_mcp_server_info(
        MCPServerInfo(
            name="github",
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(name="github:list_issues", description="List issues"),
                MCPToolInfo(name="github:search_code", description="Search code"),
            ),
            enabled=True,
        )
    )

    # Setup executor instance
    executor = A2AAutoPilotExecutor()
    executor.agent = MagicMock()
    executor._active_mcp_fingerprint = "github_active_fp"

    # 3. Disable server via toggle route
    resp = client.post("/api/mcp-servers/github/toggle", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    # 4. Verify session manager eviction and close
    mock_exit_stack.aclose.assert_awaited_once()
    assert "github" not in mcp_mgr._sessions
    assert "github" not in mcp_mgr._config

    # 5. Verify probed cache updated to disabled
    cached = {info.name: info for info in get_cached_mcp_server_infos()}
    assert "github" in cached
    assert cached["github"].status == "disabled"
    assert cached["github"].enabled is False
    assert len(cached["github"].tools) == 0

    # 6. Verify executor agent was invalidated
    assert executor.agent is None
    assert executor._active_mcp_fingerprint is None


@pytest.mark.asyncio
async def test_toggle_mcp_server_re_enables_and_probes(mcp_test_env) -> None:
    store, client = mcp_test_env

    # 1. Seed disabled github server in store
    github_server = {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "enabled": False,
        "source": "user",
    }
    await store.upsert_mcp_server(github_server)

    executor = A2AAutoPilotExecutor()
    executor.agent = MagicMock()

    mock_probed_info = MCPServerInfo(
        name="github",
        transport="stdio",
        status="ok",
        tools=(MCPToolInfo(name="github:list_issues", description="List issues"),),
        enabled=True,
    )

    with patch("k8s_autopilot.mcp.preload.probe_one_mcp_server", new=AsyncMock(return_value=mock_probed_info)):
        resp = client.post("/api/mcp-servers/github/toggle", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    # Cache should have probed info
    cached = {info.name: info for info in get_cached_mcp_server_infos()}
    assert "github" in cached
    assert cached["github"].status == "ok"
    assert cached["github"].enabled is True
    assert len(cached["github"].tools) == 1

    # Executor should be invalidated
    assert executor.agent is None


@pytest.mark.asyncio
async def test_executor_fingerprint_detects_mcp_disable_and_rebuilds(tmp_path: Path) -> None:
    adapter = SqliteConfigAdapter(tmp_path / "test_exec_mcp.db")
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    # 1. Server initially enabled
    await store.upsert_mcp_server({
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "enabled": True,
        "source": "user",
    })

    executor = A2AAutoPilotExecutor()

    mock_agent_initial = MagicMock()
    mock_agent_initial.name = "k8sAutopilotAgent"
    mock_agent_rebuilt = MagicMock()
    mock_agent_rebuilt.name = "k8sAutopilotAgent"

    with patch("k8s_autopilot.agent.factory.create_deep_agent") as mock_create_agent:
        mock_create_agent.side_effect = [mock_agent_initial, mock_agent_rebuilt]

        # First call builds agent
        agent1 = executor._ensure_agent()
        assert agent1 == mock_agent_initial
        initial_fp = executor._active_mcp_fingerprint
        assert "github" in str(initial_fp)

        # Calling again with no MCP changes reuses the agent
        agent1_cached = executor._ensure_agent()
        assert agent1_cached == mock_agent_initial
        assert mock_create_agent.call_count == 1

        # Now simulate user disabling github in DB directly (bypassing settings API)
        await store.upsert_mcp_server({
            "name": "github",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "enabled": False,
            "source": "user",
        })

        # Next call to _ensure_agent should detect fingerprint divergence and rebuild!
        agent2 = executor._ensure_agent()
        assert agent2 == mock_agent_rebuilt
        assert mock_create_agent.call_count == 2
        new_fp = executor._active_mcp_fingerprint
        assert "github" not in str(new_fp)
