"""Unit tests for plugin lifecycle MCP integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
import pytest

from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.plugins.discovery import (
    install_plugin_async,
    set_plugin_enabled_async,
    uninstall_plugin_async,
)
from k8s_autopilot.plugins.models import (
    LocalPluginSource,
    MarketplacePluginEntry,
    PluginMarketplace,
)


@pytest.mark.asyncio
async def test_plugin_install_syncs_mcp_servers(tmp_path: Path) -> None:
    """Verify that installing a partner-built plugin (e.g. lseg) auto-seeds its MCP server and doesn't disrupt existing sessions."""
    adapter = SqliteConfigAdapter(tmp_path / "test_plugin_mcp.db")
    store = ConfigStore(adapter)
    await store.initialize()

    # Pre-seed an existing server in the store
    await store.upsert_mcp_server({
        "name": "prometheus-existing",
        "transport": "http",
        "url": "http://localhost:9090/mcp",
        "enabled": True,
        "source": "user",
    })

    # Create mock plugin source with .mcp.json
    plugin_dir = tmp_path / "partner-built" / "lseg"
    plugin_dir.mkdir(parents=True)
    mcp_json_content = {
        "mcpServers": {
            "lseg": {
                "type": "http",
                "url": "https://api.analytics.lseg.com/lfa/mcp/server-cl"
            }
        }
    }
    (plugin_dir / ".mcp.json").write_text(json.dumps(mcp_json_content), encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "lseg", "version": "1.0.0", "description": "LSEG partner plugin"}),
        encoding="utf-8",
    )

    mock_entry = MarketplacePluginEntry(
        name="lseg",
        display_name="LSEG Analytics",
        description="LSEG Financial Analytics",
        source=LocalPluginSource(source_type="local", path=str(plugin_dir)),
    )
    mock_marketplace = PluginMarketplace(
        name="partner-built",
        root=tmp_path,
        manifest_path=tmp_path / "marketplace.json",
        metadata={},
        plugins=(mock_entry,),
    )

    with patch(
        "k8s_autopilot.plugins.discovery._resolve_marketplace_and_entry_async",
        return_value=(mock_marketplace, mock_entry),
    ), patch(
        "k8s_autopilot.plugins.discovery.materialize_plugin_source",
        return_value=plugin_dir,
    ):
        # Install plugin
        instance = await install_plugin_async("lseg@partner-built", store=store)
        assert instance is not None

        # Verify lseg MCP server was auto-seeded into DB
        lseg_server = await store.get_mcp_server("lseg")
        assert lseg_server is not None
        assert lseg_server["name"] == "lseg"
        assert lseg_server["transport"] == "http"
        assert lseg_server["url"] == "https://api.analytics.lseg.com/lfa/mcp/server-cl"
        assert lseg_server["source"] == "plugin:lseg"
        assert lseg_server["enabled"] is True

        # Verify existing server was untouched
        prom = await store.get_mcp_server("prometheus-existing")
        assert prom is not None
        assert prom["enabled"] is True

        # Test disabling plugin also disables its MCP server
        await set_plugin_enabled_async("lseg@partner-built", False, store=store)
        lseg_server_disabled = await store.get_mcp_server("lseg")
        assert lseg_server_disabled is not None
        assert lseg_server_disabled["enabled"] is False

        # Test uninstalling cleans up MCP server
        await uninstall_plugin_async("lseg@partner-built", store=store)
        lseg_server_deleted = await store.get_mcp_server("lseg")
        assert lseg_server_deleted is None

        # Existing server remains intact
        prom_after = await store.get_mcp_server("prometheus-existing")
        assert prom_after is not None
