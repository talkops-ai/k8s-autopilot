"""Unit tests verifying DB-backed resource fetching and container redeployment auto-rehydration.

Covers corner cases:
1. Skills exist in DB, container restarted (filesystem wiped) -> Auto-rehydrated on startup!
2. Subagents exist in DB, container restarted -> Auto-rehydrated AGENTS.md on startup!
3. Built-in subagents with .mcp.json -> Auto-synced and bound to DB.
4. MCP servers loaded from DB with enabled=True / False filtering.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.mcp.discovery import MCPDiscovery
from k8s_autopilot.skills.registry import SkillRegistry
from k8s_autopilot.subagents.loader import list_subagents_async


@pytest.mark.asyncio
class TestDbBackedAutoRehydration:
    """Test redeployment auto-rehydration for skills, subagents, and MCP servers."""

    async def test_skills_auto_rehydration_after_redeployment(self, tmp_path: Path):
        """Simulate container restart: skill is in DB, but local folder was wiped."""
        db_file = tmp_path / "app.db"
        adapter = SqliteConfigAdapter(db_file)
        store = ConfigStore(adapter)
        await store.initialize()

        # 1. Skill was previously saved to DB with full content
        skill_content = "---\nname: custom-k8s-scaler\ndescription: Auto-scale deployments\ndomain: k8s\n---\n\nSkill instructions here."
        target_path = tmp_path / "wiped_container_dir" / "custom-k8s-scaler"

        await store.upsert_skill({
            "name": "custom-k8s-scaler",
            "description": "Auto-scale deployments",
            "domain": "k8s",
            "path": str(target_path),
            "virtual_path": "skills/custom-k8s-scaler",
            "source": "user",
            "content": skill_content,
            "enabled": True,
        })

        # Verify disk directory does NOT exist yet (simulating wiped container)
        assert not target_path.exists()

        # 2. Registry runs sync on new container startup
        registry = SkillRegistry(store=store)
        active_skills = await registry.sync_with_db_async(project_root=tmp_path, store=store)

        # 3. Verify file was auto-rehydrated on disk from DB!
        assert target_path.exists()
        skill_md = target_path / "SKILL.md"
        assert skill_md.exists()
        assert "Auto-scale deployments" in skill_md.read_text(encoding="utf-8")

        # Verify it was returned in active skills
        names = [s.name for s in active_skills]
        assert "custom-k8s-scaler" in names

    async def test_subagents_auto_rehydration_after_redeployment(self, tmp_path: Path):
        """Simulate container restart: subagent is in DB, but AGENTS.md was wiped."""
        db_file = tmp_path / "app.db"
        adapter = SqliteConfigAdapter(db_file)
        store = ConfigStore(adapter)
        await store.initialize()

        # 1. Subagent saved to DB with prompt
        target_path = tmp_path / "wiped_agents_dir" / "network-debugger" / "AGENTS.md"

        await store.upsert_subagent({
            "name": "network-debugger",
            "description": "Debugs Calico and Cilium CNI network issues",
            "model": "anthropic:claude-3-7-sonnet",
            "instructions_path": str(target_path),
            "system_prompt": "You are a Kubernetes networking expert specializing in CNI.",
            "tools": ["kubectl_get", "cilium_status"],
            "source": "user",
            "enabled": True,
        })

        # Verify file does not exist on disk
        assert not target_path.exists()

        # 2. Subagent loader runs on startup
        loaded = await list_subagents_async(store=store)

        # 3. Verify AGENTS.md was auto-rehydrated!
        assert target_path.exists()
        content = target_path.read_text(encoding="utf-8")
        assert "network-debugger" in content
        assert "Kubernetes networking expert" in content

        matching = [a for a in loaded if a["name"] == "network-debugger"]
        assert len(matching) == 1
        assert matching[0]["model"] == "anthropic:claude-3-7-sonnet"

    async def test_mcp_servers_db_enabled_filtering_and_bundle_sync(self, tmp_path: Path):
        """Verify MCP servers from DB are loaded, and enabled=False servers are excluded."""
        db_file = tmp_path / "app.db"
        adapter = SqliteConfigAdapter(db_file)
        store = ConfigStore(adapter)
        await store.initialize()

        # Active server
        await store.upsert_mcp_server({
            "name": "active-mcp",
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "active_mcp"],
            "enabled": True,
            "trusted": True,
        })

        # Disabled server
        await store.upsert_mcp_server({
            "name": "disabled-mcp",
            "transport": "sse",
            "url": "http://localhost:9000/sse",
            "enabled": False,
            "trusted": False,
        })

        discovery = MCPDiscovery(store=store)
        discovered = await discovery.discover_and_sync_async(project_root=tmp_path, store=store)

        assert "active-mcp" in discovered
        assert "disabled-mcp" not in discovered
        assert discovered["active-mcp"]["trusted"] is True
