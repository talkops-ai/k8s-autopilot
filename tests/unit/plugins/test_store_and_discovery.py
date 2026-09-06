"""Unit tests for plugin state store, discovery, install lifecycle, and auto-rehydration."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.plugins.discovery import (
    add_marketplace_source_async,
    discover_plugins_async,
    install_plugin_async,
    list_available_plugins_async,
    remove_marketplace_async,
    set_plugin_enabled_async,
    uninstall_plugin_async,
)


@pytest.fixture
async def test_store(tmp_path: Path) -> ConfigStore:
    """Create initialized test ConfigStore backed by SQLite."""
    db_path = tmp_path / "test_store.db"
    adapter = SqliteConfigAdapter(db_path)
    store = ConfigStore(adapter)
    await store.initialize()
    return store


@pytest.fixture
def sample_marketplace(tmp_path: Path) -> Path:
    """Create a local marketplace directory tree fixture."""
    m_dir = tmp_path / "sample_marketplace"
    m_dir.mkdir(parents=True, exist_ok=True)

    # Plugin 1: k8s-linter
    p1_dir = m_dir / "plugins" / "k8s-linter"
    p1_dir.mkdir(parents=True, exist_ok=True)
    (p1_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "k8s-linter",
                "version": "1.0.0",
                "displayName": "Kubernetes Linter",
                "description": "Lints manifests for security issues",
                "skills": ["./skills"],
            }
        ),
        encoding="utf-8",
    )
    p1_skills = p1_dir / "skills" / "lint"
    p1_skills.mkdir(parents=True, exist_ok=True)
    (p1_skills / "SKILL.md").write_text(
        "---\nname: lint\ndescription: Lints manifests for security issues\n---\n# Linter skill",
        encoding="utf-8",
    )

    # Plugin 2: k8s-diagnostics (remains available / uninstalled)
    p2_dir = m_dir / "plugins" / "k8s-diagnostics"
    p2_dir.mkdir(parents=True, exist_ok=True)
    (p2_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "k8s-diagnostics",
                "version": "1.0.0",
                "displayName": "Kubernetes Diagnostics",
                "description": "Diagnoses cluster health",
                "skills": ["./skills"],
            }
        ),
        encoding="utf-8",
    )
    p2_skills = p2_dir / "skills" / "diag"
    p2_skills.mkdir(parents=True, exist_ok=True)
    (p2_skills / "SKILL.md").write_text(
        "---\nname: diag\ndescription: Diagnoses cluster health\n---\n# Diag skill",
        encoding="utf-8",
    )

    # marketplace.json
    (m_dir / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "official-tools",
                "metadata": {"pluginRoot": "./plugins"},
                "plugins": [
                    {
                        "name": "k8s-linter",
                        "displayName": "Kubernetes Linter",
                        "description": "Lints manifests for security issues",
                        "source": "./plugins/k8s-linter",
                    },
                    {
                        "name": "k8s-diagnostics",
                        "displayName": "Kubernetes Diagnostics",
                        "description": "Diagnoses cluster health",
                        "source": "./plugins/k8s-diagnostics",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return m_dir


@pytest.mark.asyncio
async def test_full_plugin_lifecycle(test_store: ConfigStore, sample_marketplace: Path) -> None:
    """Test Add Marketplace -> Discover -> Install -> Enable/Disable -> Uninstall."""
    # 1. Add marketplace
    marketplace = await add_marketplace_source_async(
        str(sample_marketplace), store=test_store
    )
    assert marketplace.name == "official-tools"
    assert len(marketplace.plugins) == 2

    # Verify marketplace persisted to DB
    db_marketplaces = await test_store.list_marketplaces()
    assert len(db_marketplaces) == 1
    assert db_marketplaces[0]["name"] == "official-tools"

    # 2. Discover available plugins (both are uninstalled)
    available = await list_available_plugins_async(store=test_store)
    assert len(available) == 2
    assert any(p["plugin_id"] == "k8s-linter@official-tools" for p in available)
    assert any(p["plugin_id"] == "k8s-diagnostics@official-tools" for p in available)

    # 3. Install plugin (centralized install, default global scope)
    instance = await install_plugin_async(
        "k8s-linter@official-tools",
        store=test_store,
    )
    assert instance.name == "k8s-linter"
    assert instance.marketplace == "official-tools"
    assert instance.version == "1.0.0"
    assert len(instance.inventory.skills) >= 1

    # Verify plugin record in DB
    db_plugins = await test_store.list_plugins()
    assert len(db_plugins) == 1
    assert db_plugins[0]["plugin_id"] == "k8s-linter@official-tools"
    assert db_plugins[0]["enabled"] is True
    assert db_plugins[0]["display_name"] == "Kubernetes Linter"

    # 3b. Verify Discover tab excludes installed plugin
    available_after_install = await list_available_plugins_async(store=test_store)
    assert len(available_after_install) == 1
    assert available_after_install[0]["plugin_id"] == "k8s-diagnostics@official-tools"

    # With include_installed=True, all plugins are returned
    all_plugins = await list_available_plugins_async(store=test_store, include_installed=True)
    assert len(all_plugins) == 2
    installed_item = next(p for p in all_plugins if p["plugin_id"] == "k8s-linter@official-tools")
    assert installed_item["installed"] is True

    # 3c. Verify SkillRegistry and PluginSkillsMiddleware reflect installed plugin skills
    from k8s_autopilot.skills.registry import SkillRegistry
    from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
    from unittest.mock import MagicMock

    registry = SkillRegistry.get_instance(store=test_store)
    sources = registry.get_sources_for_middleware()
    assert any(s[2] == "k8s-linter@official-tools" for s in sources if len(s) == 3)
    # Ensure uninstalled marketplace clone (k8s-diagnostics) is NOT in middleware sources
    assert not any("k8s-diagnostics" in str(s) for s in sources)

    mw = PluginSkillsMiddleware(sources=sources)
    state: dict = {}
    update = mw.before_agent(state, runtime=MagicMock(), config=MagicMock())
    assert update is not None
    loaded_skills = [s["name"] for s in update["skills_metadata"]]
    assert any("k8s-linter@official-tools" in s for s in loaded_skills)
    assert not any("k8s-diagnostics" in s for s in loaded_skills)

    # 4. Discover installed plugins
    discovered = await discover_plugins_async(store=test_store)
    assert len(discovered.plugins) == 1
    assert discovered.plugins[0].plugin_id == "k8s-linter@official-tools"

    # 5. Disable plugin
    await set_plugin_enabled_async("k8s-linter@official-tools", False, store=test_store)
    discovered_after_disable = await discover_plugins_async(store=test_store)
    assert len(discovered_after_disable.plugins) == 0

    sources_disabled = registry.get_sources_for_middleware()
    assert not any(len(s) == 3 and s[2] == "k8s-linter@official-tools" for s in sources_disabled)

    # 6. Enable plugin
    await set_plugin_enabled_async("k8s-linter@official-tools", True, store=test_store)
    discovered_after_enable = await discover_plugins_async(store=test_store)
    assert len(discovered_after_enable.plugins) == 1

    # 7. Uninstall plugin
    uninstalled = await uninstall_plugin_async("k8s-linter@official-tools", store=test_store)
    assert uninstalled is True
    assert len(await test_store.list_plugins()) == 0

    # 7b. Verify plugin skills are completely removed from SkillRegistry and PluginSkillsMiddleware
    sources_after_uninst = registry.get_sources_for_middleware()
    assert not any("k8s-linter" in str(s) for s in sources_after_uninst)

    mw_after_uninst = PluginSkillsMiddleware(sources=sources_after_uninst)
    update_after_uninst = mw_after_uninst.before_agent({}, runtime=MagicMock(), config=MagicMock())
    loaded_after = [s["name"] for s in update_after_uninst["skills_metadata"]] if update_after_uninst else []
    assert not any("k8s-linter" in s for s in loaded_after)
    assert not any("k8s-diagnostics" in s for s in loaded_after)

    # 7c. Verify plugin is back in Discover tab after uninstallation
    available_after_uninstall = await list_available_plugins_async(store=test_store)
    assert len(available_after_uninstall) == 2
    assert any(p["plugin_id"] == "k8s-linter@official-tools" for p in available_after_uninstall)

    # 8. Remove marketplace
    removed = await remove_marketplace_async("official-tools", store=test_store)
    assert removed is True
    assert len(await test_store.list_marketplaces()) == 0


@pytest.mark.asyncio
async def test_fresh_deployment_auto_rehydration_when_cache_wiped(
    test_store: ConfigStore, sample_marketplace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that a fresh container deployment automatically rehydrates missing marketplaces and plugins from DB."""
    import shutil
    cache_root = tmp_path / "ephemeral_cache"
    monkeypatch.setenv("PLUGIN_CACHE_DIR", str(cache_root))

    # 1. Initially configure marketplace and install plugin
    await add_marketplace_source_async(str(sample_marketplace), store=test_store)
    await install_plugin_async("k8s-linter@official-tools", store=test_store)

    discovered_initial = await discover_plugins_async(store=test_store)
    assert len(discovered_initial.plugins) == 1
    assert Path(discovered_initial.plugins[0].root).exists()

    # 2. Simulate fresh container deployment: wipe the entire local filesystem cache
    shutil.rmtree(cache_root, ignore_errors=True)
    assert not cache_root.exists()

    # 3. Discover available plugins - should auto-rehydrate marketplace catalog and return uninstalled plugin
    available = await list_available_plugins_async(store=test_store)
    assert len(available) == 1
    assert available[0]["plugin_id"] == "k8s-diagnostics@official-tools"

    # 4. Discover installed plugins on startup - should auto-rehydrate plugin files onto disk
    discovered_rehydrated = await discover_plugins_async(store=test_store)
    assert len(discovered_rehydrated.plugins) == 1
    assert discovered_rehydrated.plugins[0].plugin_id == "k8s-linter@official-tools"
    assert Path(discovered_rehydrated.plugins[0].root).exists()
    assert len(discovered_rehydrated.plugins[0].inventory.skills) >= 1

