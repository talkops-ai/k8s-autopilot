"""Unit and integration tests for DB Config Store (Phase 26)."""

from __future__ import annotations

from pathlib import Path

import pytest


class TestConfigCategory:
    """Tests for ConfigCategory enum."""

    def test_enum_values(self) -> None:
        from k8s_autopilot.config.store import ConfigCategory

        assert ConfigCategory.LLM == "llm"
        assert ConfigCategory.SECURITY == "security"
        assert ConfigCategory.KUBERNETES == "kubernetes"


class TestConfigEntry:
    """Tests for ConfigEntry dataclass."""

    def test_auto_display_name(self) -> None:
        from k8s_autopilot.config.store import ConfigEntry

        entry = ConfigEntry(key="LLM_PROVIDER", value="openai")
        assert entry.display_name == "Llm Provider"

    def test_explicit_display_name(self) -> None:
        from k8s_autopilot.config.store import ConfigEntry

        entry = ConfigEntry(key="LLM_PROVIDER", value="openai", display_name="Model Provider")
        assert entry.display_name == "Model Provider"


@pytest.mark.asyncio
class TestSqliteConfigAdapter:
    """Integration tests for SQLite config adapter."""

    async def test_initialize_creates_table(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()
        # Should not raise

    async def test_set_and_get(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigCategory, ConfigEntry

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()

        entry = ConfigEntry(
            key="LLM_PROVIDER",
            value="openai",
            category=ConfigCategory.LLM,
            display_name="Model Provider",
        )
        await adapter.set(entry)

        result = await adapter.get("LLM_PROVIDER")
        assert result is not None
        assert result.value == "openai"
        assert result.category == ConfigCategory.LLM

    async def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()

        assert await adapter.get("NONEXISTENT") is None

    async def test_upsert(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigEntry

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()

        await adapter.set(ConfigEntry(key="KEY", value="v1"))
        await adapter.set(ConfigEntry(key="KEY", value="v2"))

        result = await adapter.get("KEY")
        assert result is not None
        assert result.value == "v2"

    async def test_delete(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigEntry

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()

        await adapter.set(ConfigEntry(key="TO_DELETE", value="val"))
        assert await adapter.delete("TO_DELETE") is True
        assert await adapter.get("TO_DELETE") is None
        assert await adapter.delete("ALREADY_GONE") is False

    async def test_list_by_category(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigCategory, ConfigEntry

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()

        await adapter.set(ConfigEntry(key="A", value="1", category=ConfigCategory.LLM))
        await adapter.set(ConfigEntry(key="B", value="2", category=ConfigCategory.SECURITY))
        await adapter.set(ConfigEntry(key="C", value="3", category=ConfigCategory.LLM))

        llm_entries = await adapter.list_by_category(ConfigCategory.LLM)
        assert len(llm_entries) == 2
        assert all(e.category == ConfigCategory.LLM for e in llm_entries)

    async def test_load_all(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigEntry

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        await adapter.initialize()

        await adapter.set(ConfigEntry(key="K1", value="v1"))
        await adapter.set(ConfigEntry(key="K2", value="v2"))

        all_entries = await adapter.load_all()
        assert len(all_entries) == 2


@pytest.mark.asyncio
class TestConfigStore:
    """Integration tests for ConfigStore facade."""

    async def test_initialize_and_get(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigCategory, ConfigStore

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        store = ConfigStore(adapter)
        await store.initialize()

        await store.set("TEST_KEY", "test_value", category=ConfigCategory.SYSTEM)
        result = await store.get("TEST_KEY")
        assert result == "test_value"

    async def test_resolve_env_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.manifest import ConfigOption, OptionKind
        from k8s_autopilot.config.store import ConfigStore

        monkeypatch.setenv("ENV_ONLY_KEY", "env_value")

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        store = ConfigStore(adapter)
        await store.initialize()

        option = ConfigOption(
            key="test.env_only",
            db_key="ENV_ONLY_KEY",
            group="Test",
            summary="Test env fallback",
            kind=OptionKind.STR,
            default="default_val",
        )
        value, source = await store.resolve(option)
        assert value == "env_value"
        assert "env" in source

    async def test_default_fallback(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigStore

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        store = ConfigStore(adapter)
        await store.initialize()

        result = await store.get("NONEXISTENT", default="fallback")
        assert result == "fallback"

    async def test_resolve_chain(self, tmp_path: Path) -> None:
        """Test full resolution: DB → env → default."""
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.manifest import ConfigOption, OptionKind
        from k8s_autopilot.config.store import ConfigCategory, ConfigStore

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        store = ConfigStore(adapter)
        await store.initialize()

        option = ConfigOption(
            key="test.resolve",
            db_key="TEST_RESOLVE_KEY",
            group="Test",
            summary="Test resolution",
            kind=OptionKind.STR,
            default="manifest_default",
        )

        # 1. No DB, no env → manifest default
        value, source = await store.resolve(option)
        assert value == "manifest_default"
        assert source == "default"

        # 2. Write to DB → DB wins
        await store.set("TEST_RESOLVE_KEY", "db_value", category=ConfigCategory.SYSTEM)
        value2, source2 = await store.resolve(option)
        assert value2 == "db_value"
        assert source2 == "db"

    async def test_delete(self, tmp_path: Path) -> None:
        from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
        from k8s_autopilot.config.store import ConfigStore

        adapter = SqliteConfigAdapter(tmp_path / "test_config.db")
        store = ConfigStore(adapter)
        await store.initialize()

        await store.set("TO_DELETE", "val")
        assert await store.delete("TO_DELETE") is True
        assert await store.get("TO_DELETE") is None
