"""Integration tests for PostgreSQL Config Adapter using POSTGRES_URI from .env."""

from __future__ import annotations

import os
import pytest
from pathlib import Path

from k8s_autopilot.config.adapters.postgres import PostgresConfigAdapter
from k8s_autopilot.config.store import ConfigCategory, ConfigEntry

POSTGRES_URI = os.environ.get(
    "POSTGRES_URI",
    "postgresql://k8s_autopilot:k8s_autopilot_dev@localhost:5432/k8s_autopilot?sslmode=disable",
)


@pytest.mark.asyncio
class TestPostgresAdapterIntegration:
    """Integration test suite against live PostgreSQL database."""

    @pytest.fixture(autouse=True)
    async def setup_adapter(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        try:
            await adapter.initialize()
        except Exception as e:
            pytest.skip(f"PostgreSQL not accessible at {POSTGRES_URI}: {e}")
        return adapter

    async def test_postgres_config_store_crud(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        test_key = "TEST_INTEGRATION_MODEL"
        entry = ConfigEntry(
            key=test_key,
            value="google_genai:gemini-3.7-flash",
            category=ConfigCategory.MODELS,
            display_name="Test Model",
        )
        await adapter.set(entry)

        fetched = await adapter.get(test_key)
        assert fetched is not None
        assert fetched.value == "google_genai:gemini-3.7-flash"

        deleted = await adapter.delete(test_key)
        assert deleted is True
        assert await adapter.get(test_key) is None

    async def test_postgres_model_preferences(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        prefs = {
            "default_model": "anthropic:claude-3-7-sonnet",
            "recent_models": ["anthropic:claude-3-7-sonnet", "google_genai:gemini-3.7-flash"],
            "effort_by_model": {"anthropic:claude-3-7-sonnet": "high"},
            "provider_configs": {"anthropic": {"enabled": True}},
        }
        await adapter.save_model_preferences(prefs)

        loaded = await adapter.get_model_preferences()
        assert loaded["default_model"] == "anthropic:claude-3-7-sonnet"
        assert "anthropic:claude-3-7-sonnet" in loaded["recent_models"]
        assert loaded["effort_by_model"].get("anthropic:claude-3-7-sonnet") == "high"

    async def test_postgres_mcp_servers(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        name = "test-pg-mcp"
        server = {
            "name": name,
            "transport": "stdio",
            "command": "node",
            "args": ["server.js"],
            "url": None,
            "env": {"DEBUG": "true"},
            "headers": {},
            "source": "project",
            "enabled": True,
            "trusted": True,
        }
        await adapter.upsert_mcp_server(server)

        fetched = await adapter.get_mcp_server(name)
        assert fetched is not None
        assert fetched["trusted"] is True
        assert fetched["args"] == ["server.js"]

        await adapter.delete_mcp_server(name)
        assert await adapter.get_mcp_server(name) is None

    async def test_postgres_marketplaces(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        m_name = "test-pg-marketplace"
        marketplace = {
            "name": m_name,
            "source_type": "github",
            "source_value": "talkops-ai/pg-plugins",
            "install_location": "/tmp/pg-plugins",
            "ref": "main",
            "plugin_count": 3,
            "is_team": True,
        }
        await adapter.upsert_marketplace(marketplace)

        marketplaces = await adapter.list_marketplaces()
        matching = [m for m in marketplaces if m["name"] == m_name]
        assert len(matching) == 1
        assert matching[0]["source_type"] == "github"
        assert matching[0]["plugin_count"] == 3
        assert matching[0]["is_team"] is True

        single = await adapter.get_marketplace(m_name)
        assert single is not None
        assert single["name"] == m_name

        deleted = await adapter.delete_marketplace(m_name)
        assert deleted is True
        assert await adapter.get_marketplace(m_name) is None

    async def test_postgres_plugins(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        p_id = "test-plugin@marketplace"
        plugin = {
            "plugin_id": p_id,
            "name": "test-plugin",
            "marketplace": "marketplace",
            "version": "2.0.0",
            "display_name": "Test Postgres Plugin",
            "description": "Integration testing plugin",
            "author": "TalkOps Team",
            "skill_count": 2,
            "skill_names": ["skill1", "skill2"],
            "mcp_server_names": ["mcp1"],
            "install_path": "/plugins/test",
            "scope": "project",
            "source_type": "github",
            "source_value": "https://github.com/test/plugin",
            "enabled": True,
            "config": {"k": "v"},
        }
        await adapter.upsert_plugin(plugin)

        plugins = await adapter.list_plugins()
        matching = [p for p in plugins if p["plugin_id"] == p_id]
        assert len(matching) == 1
        assert matching[0]["version"] == "2.0.0"
        assert matching[0]["display_name"] == "Test Postgres Plugin"
        assert matching[0]["skill_count"] == 2
        assert "skill1" in matching[0]["skill_names"]

        single = await adapter.get_plugin(p_id)
        assert single is not None
        assert single["display_name"] == "Test Postgres Plugin"

        await adapter.delete_plugin(p_id)

    async def test_postgres_skills(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        skill_name = "test-pg-skill"
        skill = {
            "name": skill_name,
            "description": "Postgres test skill",
            "domain": "testing",
            "path": "/skills/test/SKILL.md",
            "virtual_path": "skills/test",
            "source": "project",
            "enabled": True,
        }
        await adapter.upsert_skill(skill)

        skills = await adapter.list_skills()
        matching = [s for s in skills if s["name"] == skill_name]
        assert len(matching) == 1
        assert matching[0]["description"] == "Postgres test skill"

        await adapter.delete_skill(skill_name)

    async def test_postgres_subagents(self):
        adapter = PostgresConfigAdapter(POSTGRES_URI)
        await adapter.initialize()

        agent_name = "test-pg-subagent"
        subagent = {
            "name": agent_name,
            "description": "Postgres subagent",
            "model": "google_genai:gemini-3.7-flash",
            "instructions_path": "/agents/test.md",
            "tools": ["tool_a", "tool_b"],
            "source": "built-in",
            "enabled": True,
        }
        await adapter.upsert_subagent(subagent)

        subagents = await adapter.list_subagents()
        matching = [a for a in subagents if a["name"] == agent_name]
        assert len(matching) == 1
        assert matching[0]["tools"] == ["tool_a", "tool_b"]

        await adapter.delete_subagent(agent_name)
