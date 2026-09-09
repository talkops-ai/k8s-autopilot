"""Unit tests for PluginSkillsMiddleware dynamic skill injection and uninstalled skill exclusion."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
from k8s_autopilot.plugins.discovery import (
    add_marketplace_source_async,
    install_plugin_async,
    uninstall_plugin_async,
)
from k8s_autopilot.skills.registry import SkillRegistry


@pytest.fixture
async def test_store(tmp_path: Path) -> ConfigStore:
    """Create initialized test ConfigStore backed by SQLite."""
    db_path = tmp_path / "test_store.db"
    adapter = SqliteConfigAdapter(db_path)
    store = ConfigStore(adapter)
    await store.initialize()
    return store


@pytest.fixture
def sample_marketplace_tree(tmp_path: Path) -> Path:
    """Create a sample marketplace tree fixture."""
    m_dir = tmp_path / "sample_marketplace"
    m_dir.mkdir(parents=True, exist_ok=True)

    p1_dir = m_dir / "plugins" / "argocd-expert"
    p1_dir.mkdir(parents=True, exist_ok=True)
    (p1_dir / "plugin.json").write_text(
        json.dumps({
            "name": "argocd-expert",
            "version": "1.0.0",
            "displayName": "ArgoCD Expert",
            "description": "ArgoCD GitOps skills",
            "skills": ["./skills"],
        }),
        encoding="utf-8",
    )
    p1_skill = p1_dir / "skills" / "sync-apps"
    p1_skill.mkdir(parents=True, exist_ok=True)
    (p1_skill / "SKILL.md").write_text(
        "---\nname: sync-apps\ndescription: Syncs ArgoCD applications\n---\n# Sync apps workflow",
        encoding="utf-8",
    )

    (m_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "gitops-hub",
            "metadata": {"pluginRoot": "./plugins"},
            "plugins": [
                {
                    "name": "argocd-expert",
                    "displayName": "ArgoCD Expert",
                    "description": "ArgoCD GitOps skills",
                    "source": "./plugins/argocd-expert",
                }
            ],
        }),
        encoding="utf-8",
    )
    return m_dir


@pytest.mark.asyncio
async def test_plugin_skills_middleware_excludes_uninstalled_skills(
    test_store: ConfigStore, sample_marketplace_tree: Path
) -> None:
    """Verify PluginSkillsMiddleware only injects installed plugin skills and excludes uninstalled ones."""
    # 1. Add marketplace
    await add_marketplace_source_async(str(sample_marketplace_tree), store=test_store)

    registry = SkillRegistry.get_instance(store=test_store)

    # 2. Before installation: uninstalled marketplace skills must NOT be in middleware sources
    sources_before = registry.get_sources_for_middleware()
    assert not any("argocd-expert" in str(s) for s in sources_before)

    mw_before = PluginSkillsMiddleware(sources=sources_before)
    state_before: dict = {}
    update_before = mw_before.before_agent(state_before, runtime=MagicMock(), config=MagicMock())
    skills_before = [s["name"] for s in update_before["skills_metadata"]] if update_before else []
    assert not any("sync-apps" in s for s in skills_before)

    # 3. Install plugin
    await install_plugin_async("argocd-expert@gitops-hub", store=test_store)

    sources_installed = registry.get_sources_for_middleware()
    assert any(len(s) == 3 and s[2] == "argocd-expert@gitops-hub" for s in sources_installed)

    mw_installed = PluginSkillsMiddleware(sources=sources_installed)
    state_installed: dict = {}
    update_installed = mw_installed.before_agent(state_installed, runtime=MagicMock(), config=MagicMock())
    assert update_installed is not None
    skills_installed = [s["name"] for s in update_installed["skills_metadata"]]
    assert any("argocd-expert@gitops-hub:sync-apps" in s for s in skills_installed)

    # Test modify_request prompt generation
    from langchain_core.messages import SystemMessage
    mock_request = MagicMock()
    mock_request.state = {"skills_metadata": update_installed["skills_metadata"]}
    mock_request.system_message = SystemMessage(content="System prompt base.")
    mw_installed.modify_request(mock_request)
    assert mock_request.override.called
    override_kwargs = mock_request.override.call_args.kwargs
    assert "sync-apps" in str(override_kwargs["system_message"])

    # 4. Uninstall plugin
    await uninstall_plugin_async("argocd-expert@gitops-hub", store=test_store)

    # Verify DB tables are completely clean of the uninstalled plugin and its skills
    db_plugins_after = await test_store.list_plugins()
    assert len(db_plugins_after) == 0

    db_skills_after = await test_store.list_skills()
    assert not any("sync-apps" in s["name"] or "argocd-expert" in s.get("path", "") for s in db_skills_after)

    # Verify sources and state immediately exclude the skill
    sources_after = registry.get_sources_for_middleware()
    assert not any("argocd-expert" in str(s) for s in sources_after)

    mw_after = PluginSkillsMiddleware(sources=sources_after)
    state_after: dict = {}
    update_after = mw_after.before_agent(state_after, runtime=MagicMock(), config=MagicMock())
    skills_after = [s["name"] for s in update_after["skills_metadata"]] if update_after else []
    assert not any("sync-apps" in s for s in skills_after)
    assert not any("argocd-expert" in s for s in skills_after)
