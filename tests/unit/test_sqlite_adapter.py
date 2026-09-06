"""Unit and integration tests for SQLite Config Adapter across all 7 tables."""

from __future__ import annotations

import pytest
from pathlib import Path

from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigCategory, ConfigEntry, ConfigStore


@pytest.mark.asyncio
class TestSqliteAdapterAllTables:
    """Test all 7 entity tables in SQLite adapter."""

    async def test_initialize_creates_tables(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        adapter = SqliteConfigAdapter(db_path)
        await adapter.initialize()
        assert db_path.exists()

    async def test_config_store_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        entry = ConfigEntry(
            key="MODEL",
            value="google_genai:gemini-3.7-flash",
            category=ConfigCategory.MODELS,
            display_name="Active Model",
        )
        await adapter.set(entry)

        fetched = await adapter.get("MODEL")
        assert fetched is not None
        assert fetched.value == "google_genai:gemini-3.7-flash"
        assert fetched.category == ConfigCategory.MODELS

        all_entries = await adapter.load_all()
        assert len(all_entries) == 1

        deleted = await adapter.delete("MODEL")
        assert deleted is True
        assert await adapter.get("MODEL") is None

    async def test_model_preferences_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        prefs = {
            "default_model": "anthropic:claude-3-7-sonnet",
            "recent_models": ["anthropic:claude-3-7-sonnet", "google_genai:gemini-2.5-pro"],
            "effort_by_model": {"anthropic:claude-3-7-sonnet": "high"},
            "provider_configs": {"openai": {"enabled": True}},
        }
        await adapter.save_model_preferences(prefs)

        loaded = await adapter.get_model_preferences()
        assert loaded["default_model"] == "anthropic:claude-3-7-sonnet"
        assert len(loaded["recent_models"]) == 2
        assert loaded["effort_by_model"]["anthropic:claude-3-7-sonnet"] == "high"

    async def test_mcp_servers_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        server = {
            "name": "k8s-mcp",
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "k8s_mcp"],
            "url": None,
            "env": {"KUBECONFIG": "/tmp/kube"},
            "headers": {},
            "source": "user",
            "enabled": True,
            "trusted": True,
        }
        await adapter.upsert_mcp_server(server)

        servers = await adapter.list_mcp_servers()
        assert len(servers) == 1
        assert servers[0]["name"] == "k8s-mcp"
        assert servers[0]["trusted"] is True
        assert servers[0]["args"] == ["-m", "k8s_mcp"]

        single = await adapter.get_mcp_server("k8s-mcp")
        assert single is not None
        assert single["name"] == "k8s-mcp"

        deleted = await adapter.delete_mcp_server("k8s-mcp")
        assert deleted is True
        assert await adapter.get_mcp_server("k8s-mcp") is None

    async def test_marketplaces_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        marketplace = {
            "name": "devops-tools",
            "source_type": "github",
            "source_value": "talkops-ai/devops-plugins",
            "install_location": "/cache/devops-tools",
            "ref": "main",
            "plugin_count": 5,
            "is_team": True,
        }
        await adapter.upsert_marketplace(marketplace)

        marketplaces = await adapter.list_marketplaces()
        assert len(marketplaces) == 1
        assert marketplaces[0]["name"] == "devops-tools"
        assert marketplaces[0]["plugin_count"] == 5
        assert marketplaces[0]["is_team"] is True

        single = await adapter.get_marketplace("devops-tools")
        assert single is not None
        assert single["name"] == "devops-tools"
        assert single["source_type"] == "github"

        deleted = await adapter.delete_marketplace("devops-tools")
        assert deleted is True
        assert await adapter.get_marketplace("devops-tools") is None
        assert len(await adapter.list_marketplaces()) == 0

    async def test_plugins_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        plugin = {
            "plugin_id": "k8s-toolkit@community",
            "name": "k8s-toolkit",
            "marketplace": "community",
            "version": "1.2.0",
            "display_name": "Kubernetes Toolkit",
            "description": "Comprehensive Kubernetes management",
            "author": "TalkOps",
            "skill_count": 3,
            "skill_names": ["pod-debugger", "helm-linter", "netpol-checker"],
            "mcp_server_names": ["k8s-mcp"],
            "install_path": "/plugins/k8s-toolkit",
            "scope": "user",
            "source_type": "github",
            "source_value": "https://github.com/example/k8s-toolkit",
            "enabled": True,
            "config": {"auto_upgrade": True},
        }
        await adapter.upsert_plugin(plugin)

        plugins = await adapter.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["plugin_id"] == "k8s-toolkit@community"
        assert plugins[0]["display_name"] == "Kubernetes Toolkit"
        assert plugins[0]["skill_count"] == 3
        assert "pod-debugger" in plugins[0]["skill_names"]
        assert "k8s-mcp" in plugins[0]["mcp_server_names"]
        assert plugins[0]["config"]["auto_upgrade"] is True

        single = await adapter.get_plugin("k8s-toolkit@community")
        assert single is not None
        assert single["display_name"] == "Kubernetes Toolkit"

        deleted = await adapter.delete_plugin("k8s-toolkit@community")
        assert deleted is True
        assert len(await adapter.list_plugins()) == 0

    async def test_skills_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        skill = {
            "name": "k8s-pod-debugger",
            "description": "Diagnose crashing Kubernetes pods",
            "domain": "k8s",
            "path": "/skills/k8s-pod-debugger/SKILL.md",
            "virtual_path": "skills/k8s-pod-debugger",
            "source": "built-in",
            "enabled": True,
        }
        await adapter.upsert_skill(skill)

        skills = await adapter.list_skills()
        assert len(skills) == 1
        assert skills[0]["name"] == "k8s-pod-debugger"
        assert skills[0]["domain"] == "k8s"

        deleted = await adapter.delete_skill("k8s-pod-debugger")
        assert deleted is True
        assert len(await adapter.list_skills()) == 0

    async def test_subagents_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        subagent = {
            "name": "cluster-auditor",
            "description": "Audits Kubernetes RBAC and network policies",
            "model": "anthropic:claude-3-7-sonnet",
            "instructions_path": "/agents/cluster-auditor.md",
            "tools": ["kubectl_get", "kubectl_describe"],
            "source": "built-in",
            "enabled": True,
        }
        await adapter.upsert_subagent(subagent)

        subagents = await adapter.list_subagents()
        assert len(subagents) == 1
        assert subagents[0]["name"] == "cluster-auditor"
        assert subagents[0]["tools"] == ["kubectl_get", "kubectl_describe"]

        deleted = await adapter.delete_subagent("cluster-auditor")
        assert deleted is True
        assert len(await adapter.list_subagents()) == 0

    async def test_agent_instructions_crud(self, tmp_path: Path):
        adapter = SqliteConfigAdapter(tmp_path / "test.db")
        await adapter.initialize()

        instruction = {
            "scope": "default",
            "file_path": "/path/to/AGENTS.md",
            "checksum": "sha256:abc123def456",
        }
        await adapter.upsert_agent_instruction(instruction)

        instructions = await adapter.list_agent_instructions()
        assert len(instructions) == 1
        assert instructions[0]["scope"] == "default"
        assert instructions[0]["checksum"] == "sha256:abc123def456"
