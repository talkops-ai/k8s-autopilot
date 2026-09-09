"""Unit tests for tiered cache invalidation and dynamic model switching.

Verifies:
1. POST /api/models/select updates DB and Settings without invalidating compiled agent or closing MCP sessions.
2. PUT /api/settings only invalidates MCP sessions / agent graphs when MCP or storage settings change.
3. Executor._ensure_agent does not recompile the agent graph on model or effort changes.
4. create_model caching reuses instances and clear_model_cache clears them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.api.settings_routes import create_settings_routes, set_config_store
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.mcp.session_manager import MCPSessionManager
from k8s_autopilot.model.factory import clear_model_cache, create_model
from k8s_autopilot.server.executor import A2AAutoPilotExecutor


@pytest.fixture
async def test_env(tmp_path: Path):
    adapter = SqliteConfigAdapter(tmp_path / "test_tiered_invalidation.db")
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    routes = create_settings_routes()
    app = Starlette(routes=routes)
    client = TestClient(app)

    # Initialize a mock executor with a pre-compiled agent
    executor = A2AAutoPilotExecutor()
    mock_agent = MagicMock()
    mock_agent.name = "k8s-autopilot"
    executor.agent = mock_agent

    yield store, client, executor
    A2AAutoPilotExecutor._instances.clear()
    clear_model_cache()


def test_create_model_caching():
    """Verify create_model returns cached instance and clear_model_cache flushes it."""
    clear_model_cache()
    res1 = create_model("google_genai:gemini-2.5-flash")
    res2 = create_model("google_genai:gemini-2.5-flash")
    assert res1 is res2

    clear_model_cache()
    res3 = create_model("google_genai:gemini-2.5-flash")
    assert res3 is not res1


def test_ensure_agent_does_not_recompile_on_model_or_effort_change():
    """Verify changing model or effort in _ensure_agent does not recompile the agent graph."""
    executor = A2AAutoPilotExecutor()
    with patch("k8s_autopilot.agent.create_k8s_autopilot_agent") as mock_create:
        initial_agent = MagicMock()
        mock_create.return_value = (initial_agent, MagicMock())

        # First call: compiles graph
        agent1 = executor._ensure_agent(requested_model="google_genai:gemini-3.6-flash", requested_effort="low")
        assert mock_create.call_count == 1
        assert agent1 is initial_agent
        assert executor._active_model == "google_genai:gemini-3.6-flash"
        assert executor._active_effort == "low"

        # Second call with different model: MUST NOT recompile!
        agent2 = executor._ensure_agent(requested_model="google_genai:gemini-3.5-flash", requested_effort="high")
        assert mock_create.call_count == 1  # Still 1! No second compilation
        assert agent2 is agent1  # Reused same compiled graph instance!
        assert executor._active_model == "google_genai:gemini-3.5-flash"
        assert executor._active_effort == "high"


@pytest.mark.asyncio
async def test_select_model_preserves_agent_and_mcp_sessions(test_env):
    """Verify POST /api/models/select updates DB and Settings without invalidating agent or closing MCP."""
    store, client, executor = test_env

    # Setup active mock MCP session
    mcp_mgr = MCPSessionManager.get_instance({"k8s": {"transport": "stdio", "command": "echo"}})
    mock_exit_stack = AsyncMock()
    session_entry = MagicMock()
    session_entry.exit_stack = mock_exit_stack
    mcp_mgr._sessions["k8s"] = session_entry

    compiled_agent = executor.agent
    assert compiled_agent is not None

    resp = client.post(
        "/api/models/select",
        json={"model": "google_genai:gemini-3.5-flash", "effort": "medium"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["model"] == "google_genai:gemini-3.5-flash"

    # DB updated
    db_model = await store.get("MODEL")
    assert db_model == "google_genai:gemini-3.5-flash"

    # Settings updated
    assert get_settings().model == "google_genai:gemini-3.5-flash"

    # Agent graph preserved!
    assert executor.agent is compiled_agent

    # MCP session NOT closed!
    mock_exit_stack.aclose.assert_not_awaited()
    assert "k8s" in mcp_mgr._sessions


@pytest.mark.asyncio
async def test_put_settings_tiered_invalidation(test_env):
    """Verify dynamic settings do not invalidate graph, but MCP settings do."""
    store, client, executor = test_env

    # 1. Update non-MCP dynamic setting (e.g. k8s.namespace)
    compiled_agent = executor.agent
    resp = client.put("/api/settings", json=[{"key": "k8s.namespace", "value": "production"}])
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1
    # Agent graph preserved
    assert executor.agent is compiled_agent
    single_resp = client.get("/api/settings/k8s.namespace")
    assert single_resp.status_code == 200
    assert single_resp.json()["value"] == "production"

    # 2. Update MCP setting
    resp = client.put("/api/settings", json=[{"key": "mcp.enabled", "value": "true"}])
    assert resp.status_code == 200
    # MCP setting change invalidated agent graph
    assert executor.agent is None
