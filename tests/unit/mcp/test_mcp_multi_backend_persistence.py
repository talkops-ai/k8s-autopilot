"""Unit tests for multi-backend persistence of MCP servers (SQLite & PostgreSQL)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.adapters.postgres import PostgresConfigAdapter
from k8s_autopilot.config.store import ConfigStore


@pytest.mark.asyncio
async def test_sqlite_mcp_server_crud(tmp_path: Path) -> None:
    """Verify full CRUD lifecycle of MCP servers in SQLite."""
    adapter = SqliteConfigAdapter(tmp_path / "test_mcp.db")
    store = ConfigStore(adapter)
    await store.initialize()

    server_record = {
        "name": "prometheus-mcp",
        "transport": "http",
        "url": "http://localhost:9090/mcp",
        "command": None,
        "args": [],
        "env": {"DEBUG": "true"},
        "headers": {"Authorization": "Bearer test"},
        "source": "user",
        "auth_token_env_var": "PROM_TOKEN",
        "disabled_tools": ["delete_series"],
        "allowed_tools": ["query", "query_range"],
        "enabled": True,
        "trusted": True,
    }

    # 1. Upsert
    await store.upsert_mcp_server(server_record)

    # 2. Get
    fetched = await store.get_mcp_server("prometheus-mcp")
    assert fetched is not None
    assert fetched["name"] == "prometheus-mcp"
    assert fetched["transport"] == "http"
    assert fetched["url"] == "http://localhost:9090/mcp"
    assert fetched["headers"] == {"Authorization": "Bearer test"}
    assert fetched["disabled_tools"] == ["delete_series"]
    assert fetched["allowed_tools"] == ["query", "query_range"]
    assert fetched["enabled"] is True
    assert fetched["trusted"] is True

    # 3. List
    servers = await store.list_mcp_servers()
    assert len(servers) == 1
    assert servers[0]["name"] == "prometheus-mcp"

    # 4. Update (disable)
    fetched["enabled"] = False
    fetched["disabled_tools"] = ["*"]
    await store.upsert_mcp_server(fetched)

    updated = await store.get_mcp_server("prometheus-mcp")
    assert updated is not None
    assert updated["enabled"] is False
    assert updated["disabled_tools"] == ["*"]

    # 5. Delete
    deleted = await store.delete_mcp_server("prometheus-mcp")
    assert deleted is True

    # 6. Verify gone
    after_del = await store.get_mcp_server("prometheus-mcp")
    assert after_del is None
    assert len(await store.list_mcp_servers()) == 0


@pytest.mark.asyncio
async def test_postgres_mcp_server_crud_mocked() -> None:
    """Verify PostgreSQL adapter mcp_server CRUD queries and parameter bindings."""
    adapter = PostgresConfigAdapter("postgresql://user:pass@localhost:5432/db")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.execute.return_value = mock_cursor
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None

    adapter._connect = MagicMock(return_value=mock_conn)

    server = {
        "name": "tempo-mcp",
        "transport": "http",
        "url": "http://localhost:3200",
        "disabled_tools": ["purge"],
        "allowed_tools": [],
        "enabled": True,
        "trusted": False,
    }

    # Upsert
    await adapter.upsert_mcp_server(server)
    assert mock_conn.execute.called
    assert mock_conn.commit.called

    # Get
    mock_cursor.fetchone.return_value = {
        "name": "tempo-mcp",
        "transport": "http",
        "command": None,
        "args": "[]",
        "url": "http://localhost:3200",
        "env": "{}",
        "headers": "{}",
        "source": "user",
        "auth_token_env_var": None,
        "disabled_tools": '["purge"]',
        "allowed_tools": "[]",
        "enabled": True,
        "trusted": False,
    }
    result = await adapter.get_mcp_server("tempo-mcp")
    assert result is not None
    assert result["name"] == "tempo-mcp"
    assert result["disabled_tools"] == ["purge"]

    # Delete
    mock_cursor.rowcount = 1
    deleted = await adapter.delete_mcp_server("tempo-mcp")
    assert deleted is True
