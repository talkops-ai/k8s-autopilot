"""Unit tests for all 8 MCP REST API endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.api.settings_routes import create_settings_routes, set_config_store
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore


@pytest.fixture
async def mcp_app_client(tmp_path: Path):
    adapter = SqliteConfigAdapter(tmp_path / "test_mcp_api.db")
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    routes = create_settings_routes()
    app = Starlette(routes=routes)
    return TestClient(app)


def test_mcp_servers_crud_endpoints(mcp_app_client: TestClient) -> None:
    # 1. Upsert single server via POST /api/mcp-servers
    server_data = {
        "name": "prometheus-mcp-server",
        "transport": "http",
        "url": "http://0.0.0.0:8767/mcp",
        "command": None,
        "args": [],
        "env": {},
        "headers": {},
        "disabled_tools": [],
        "allowed_tools": [],
        "enabled": True,
        "trusted": True,
        "source": "project",
    }
    resp_post = mcp_app_client.post("/api/mcp-servers", json=server_data)
    assert resp_post.status_code == 200
    assert resp_post.json()["success"] is True
    assert resp_post.json()["name"] == "prometheus-mcp-server"

    # 2. List servers via GET /api/mcp-servers
    resp_list = mcp_app_client.get("/api/mcp-servers")
    assert resp_list.status_code == 200
    servers = resp_list.json()
    assert len(servers) == 1
    assert servers[0]["name"] == "prometheus-mcp-server"
    assert servers[0]["transport"] == "http"
    assert servers[0]["url"] == "http://0.0.0.0:8767/mcp"
    assert servers[0]["enabled"] is True

    # 3. Toggle server via POST /api/mcp-servers/{name}/toggle
    resp_toggle = mcp_app_client.post(
        "/api/mcp-servers/prometheus-mcp-server/toggle",
        json={"enabled": False},
    )
    assert resp_toggle.status_code == 200
    assert resp_toggle.json()["success"] is True
    assert resp_toggle.json()["enabled"] is False

    # Verify toggle persisted
    resp_list2 = mcp_app_client.get("/api/mcp-servers")
    assert resp_list2.json()[0]["enabled"] is False

    # 4. Delete server via DELETE /api/mcp-servers/{name}
    resp_del = mcp_app_client.delete("/api/mcp-servers/prometheus-mcp-server")
    assert resp_del.status_code == 200
    assert resp_del.json()["success"] is True
    assert resp_del.json()["name"] == "prometheus-mcp-server"

    # Verify deletion
    resp_del_404 = mcp_app_client.delete("/api/mcp-servers/prometheus-mcp-server")
    assert resp_del_404.status_code == 404


def test_mcp_servers_status_and_probe_endpoints(mcp_app_client: TestClient) -> None:
    # Add a disabled server and an active server
    mcp_app_client.post(
        "/api/mcp-servers",
        json={
            "name": "disabled-server",
            "transport": "http",
            "url": "http://127.0.0.1:8767/mcp",
            "enabled": False,
        },
    )

    mock_tool = MagicMock()
    mock_tool.name = "search_docs"
    mock_tool.description = "Search LangChain documentation"
    mock_tool.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[mock_tool]))

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("langchain_mcp_adapters.sessions.create_session", return_value=mock_ctx):
        mcp_app_client.post(
            "/api/mcp-servers",
            json={
                "name": "docs-langchain",
                "transport": "http",
                "url": "https://docs.langchain.com/mcp",
                "enabled": True,
            },
        )

        # GET /api/mcp-servers/status
        resp_status = mcp_app_client.get("/api/mcp-servers/status")
        assert resp_status.status_code == 200
        status_data = resp_status.json()
        assert "total_tools" in status_data
        assert "servers" in status_data
        assert len(status_data["servers"]) == 2

        server_map = {s["name"]: s for s in status_data["servers"]}
        assert server_map["disabled-server"]["status"] == "disabled"
        assert server_map["docs-langchain"]["status"] == "ok"
        assert server_map["docs-langchain"]["tool_count"] == 1
        assert server_map["docs-langchain"]["tools"][0]["name"] == "docs-langchain:search_docs"

        # POST /api/mcp-servers/{name}/probe
        resp_probe = mcp_app_client.post("/api/mcp-servers/docs-langchain/probe")
        assert resp_probe.status_code == 200
        probe_data = resp_probe.json()
        assert probe_data["name"] == "docs-langchain"
        assert probe_data["status"] == "ok"
        assert probe_data["tool_count"] == 1


def test_mcp_raw_config_endpoints(mcp_app_client: TestClient) -> None:
    # 1. PUT /api/mcp/raw-config (Import)
    raw_config = {
        "mcpServers": {
            "prometheus-mcp-server": {
                "serverUrl": "http://0.0.0.0:8767/mcp",
                "disabledTools": [],
                "disabled": True,
            },
            "copilotkit-mcp": {
                "command": "npx",
                "args": ["-y", "mcp-remote", "https://mcp.copilotkit.ai/sse"],
                "disabledTools": [],
                "disabled": False,
            },
        }
    }

    resp_put = mcp_app_client.put("/api/mcp/raw-config", json=raw_config)
    assert resp_put.status_code == 200
    assert resp_put.json()["success"] is True
    assert "prometheus-mcp-server" in resp_put.json()["synced_servers"]
    assert "copilotkit-mcp" in resp_put.json()["synced_servers"]

    # 2. GET /api/mcp/raw-config (Export)
    resp_get = mcp_app_client.get("/api/mcp/raw-config")
    assert resp_get.status_code == 200
    exported = resp_get.json()
    assert "mcpServers" in exported
    assert "prometheus-mcp-server" in exported["mcpServers"]
    assert "copilotkit-mcp" in exported["mcpServers"]

    prom = exported["mcpServers"]["prometheus-mcp-server"]
    assert prom["serverUrl"] == "http://0.0.0.0:8767/mcp"
    assert prom["disabled"] is True

    copilot = exported["mcpServers"]["copilotkit-mcp"]
    assert copilot["command"] == "npx"
    assert copilot["disabled"] is False
