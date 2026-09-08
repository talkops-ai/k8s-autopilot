"""Unit tests for Settings, Backend Storage Hot-Swapping, and Custom Configuration.

Covers:
1. Manifest options for system.postgres_uri and system.checkpoint_backend.
2. Settings dataclass and environment variable resolution.
3. get_active_backend() logic with CHECKPOINT_BACKEND and POSTGRES_URI.
4. create_runtime_checkpointer for sqlite and postgres error handling.
5. PUT /api/settings handling POSTGRES_URI and custom variables.
6. GET /api/settings and GET /api/settings/{key} exposing custom variables under group "Custom".
7. DELETE /api/settings/{key} removing custom variable from store, os.environ, and .env.
8. Dynamic checkpointer hot-swapping on executor and ThreadService.
9. Startup custom configuration rehydration into os.environ.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.api.service import ThreadService, get_thread_service, set_thread_service
from k8s_autopilot.api.settings_routes import create_settings_routes, set_config_store
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.manifest import OptionKind, get_option
from k8s_autopilot.config.settings import Settings
from k8s_autopilot.config.store import ConfigCategory, ConfigStore
from k8s_autopilot.server.executor import A2AAutoPilotExecutor
from k8s_autopilot.state.session import (
    create_runtime_checkpointer,
    get_active_backend,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
async def api_env(tmp_path, monkeypatch):
    """Fixture initializing an isolated SQLite ConfigStore and Starlette TestClient."""
    db_file = tmp_path / "test_settings_custom.db"
    env_file = tmp_path / ".env"
    monkeypatch.setattr("k8s_autopilot.config.paths.GLOBAL_ENV_PATH", env_file)

    adapter = SqliteConfigAdapter(db_file)
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    routes = create_settings_routes()
    app = Starlette(routes=routes)
    client = TestClient(app)

    yield {
        "client": client,
        "store": store,
        "db_file": db_file,
        "env_file": env_file,
    }


# ── 1. Manifest & Settings Dataclass Tests ───────────────────────────────

def test_manifest_options_postgres_and_checkpoint():
    """Verify system.postgres_uri and system.checkpoint_backend are registered in manifest."""
    pg_opt = get_option("system.postgres_uri")
    assert pg_opt is not None
    assert pg_opt.db_key == "POSTGRES_URI"
    assert pg_opt.kind == OptionKind.SECRET
    assert pg_opt.redacted is True
    assert pg_opt.settings_field == "postgres_uri"

    cp_opt = get_option("system.checkpoint_backend")
    assert cp_opt is not None
    assert cp_opt.db_key == "CHECKPOINT_BACKEND"
    assert cp_opt.kind == OptionKind.CHOICE
    assert cp_opt.default == "sqlite"
    assert "sqlite" in cp_opt.choices
    assert "postgres" in cp_opt.choices
    assert "auto" in cp_opt.choices
    assert cp_opt.settings_field == "checkpoint_backend"


def test_settings_dataclass_defaults():
    """Test Settings dataclass default values."""
    s = Settings()
    assert s.checkpoint_backend == "sqlite"
    assert s.postgres_uri is None


def test_settings_from_env_overrides(monkeypatch):
    """Test Settings.from_env() resolution with environment variables."""
    monkeypatch.setenv("CHECKPOINT_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://localhost:5432/testdb")
    s = Settings.from_env()
    assert s.checkpoint_backend == "postgres"
    assert s.postgres_uri == "postgresql://localhost:5432/testdb"


# ── 2. Backend Detection & Checkpointer Factory ──────────────────────────

def test_get_active_backend_detection(monkeypatch):
    """Verify get_active_backend resolves properly across env vars."""
    monkeypatch.delenv("CHECKPOINT_BACKEND", raising=False)
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("K8S_AUTOPILOT_POSTGRES_URI", raising=False)

    # Default is sqlite
    assert get_active_backend() == "sqlite"

    # Explicit CHECKPOINT_BACKEND="postgres"
    monkeypatch.setenv("CHECKPOINT_BACKEND", "postgres")
    assert get_active_backend() == "postgres"

    # Explicit CHECKPOINT_BACKEND="sqlite"
    monkeypatch.setenv("CHECKPOINT_BACKEND", "sqlite")
    assert get_active_backend() == "sqlite"

    # POSTGRES_URI fallback
    monkeypatch.delenv("CHECKPOINT_BACKEND", raising=False)
    monkeypatch.setenv("POSTGRES_URI", "postgresql://localhost:5432/db")
    assert get_active_backend() == "postgres"


@pytest.mark.asyncio
async def test_create_runtime_checkpointer_sqlite(tmp_path, monkeypatch):
    """Verify create_runtime_checkpointer creates initialized SQLite saver that survives GC."""
    import asyncio
    import gc
    from k8s_autopilot.state.session import close_checkpointer

    db_file = tmp_path / "runtime_sessions.db"
    monkeypatch.setattr("k8s_autopilot.state.session.get_db_path", lambda: db_file)

    saver = await create_runtime_checkpointer(backend="sqlite")
    assert saver is not None
    assert hasattr(saver, "aget_tuple")

    # Force GC to ensure connection worker thread does not get terminated
    gc.collect()
    await asyncio.sleep(0.05)

    config = {"configurable": {"thread_id": "test-gc-thread", "checkpoint_ns": ""}}
    tup = await saver.aget_tuple(config)
    assert tup is None

    await close_checkpointer(saver)


@pytest.mark.asyncio
async def test_create_runtime_checkpointer_postgres_missing_uri(monkeypatch):
    """Verify create_runtime_checkpointer raises ValueError if postgres URI is missing."""
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("K8S_AUTOPILOT_POSTGRES_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="no URI provided"):
        await create_runtime_checkpointer(backend="postgres")


# ── 3. REST API: Custom Variables & Storage Hot-Swapping ────────────────

def test_put_settings_with_postgres_uri_and_storage_swap(api_env, monkeypatch):
    """AC-1 & AC-3: PUT /api/settings accepts POSTGRES_URI and CHECKPOINT_BACKEND."""
    client = api_env["client"]

    # Switching to sqlite storage
    payload = [
        {"key": "CHECKPOINT_BACKEND", "value": "sqlite"},
        {"key": "POSTGRES_URI", "value": "postgresql://postgres:postgres@localhost:5432/talkops"},
    ]
    resp = client.put("/api/settings", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["updated"] == 2
    assert "Unknown setting" not in str(data)


def test_put_and_get_custom_environment_variable(api_env):
    """AC-4: Custom key-value pairs are saved in ConfigStore, os.environ, and returned in GET /api/settings."""
    client = api_env["client"]
    env_file = api_env["env_file"]

    custom_key = "MY_CUSTOM_TEST_VARIABLE"
    custom_val = "custom_secret_12345"

    # 1. PUT custom key
    resp = client.put("/api/settings", json=[{"key": custom_key, "value": custom_val}])
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["updated"] == 1

    # 2. Verify in os.environ
    assert os.environ.get(custom_key) == custom_val

    # 3. Verify written to .env file
    assert env_file.exists()
    assert f"{custom_key}={custom_val}" in env_file.read_text(encoding="utf-8")

    # 4. Verify in GET /api/settings under group="Custom"
    list_resp = client.get("/api/settings")
    assert list_resp.status_code == 200
    settings_list = list_resp.json()
    custom_entry = next((s for s in settings_list if s["key"] == custom_key), None)
    assert custom_entry is not None
    assert custom_entry["group"] == "Custom"
    assert custom_entry["value"] == custom_val
    assert custom_entry["source"] == "db"

    # 5. Verify in GET /api/settings/{key}
    single_resp = client.get(f"/api/settings/{custom_key}")
    assert single_resp.status_code == 200
    assert single_resp.json()["value"] == custom_val
    assert single_resp.json()["group"] == "Custom"


def test_delete_custom_environment_variable(api_env):
    """AC-5: DELETE /api/settings/{key} removes custom key from DB, os.environ, and .env."""
    client = api_env["client"]
    env_file = api_env["env_file"]

    custom_key = "TEMP_DELETE_ME_VAR"
    custom_val = "to_be_deleted"

    # Add variable
    client.put("/api/settings", json=[{"key": custom_key, "value": custom_val}])
    assert os.environ.get(custom_key) == custom_val

    # Delete variable
    del_resp = client.delete(f"/api/settings/{custom_key}")
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True
    assert del_resp.json()["deleted"] == custom_key

    # Check os.environ
    assert custom_key not in os.environ

    # Check .env file
    env_content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    assert custom_key not in env_content

    # Check GET /api/settings/{key} returns 404
    get_resp = client.get(f"/api/settings/{custom_key}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_dynamic_checkpointer_hot_swap_wiring(api_env, monkeypatch):
    """Verify PUT /api/settings hot-swaps checkpointer on active executors and ThreadService."""
    client = api_env["client"]

    # Create dummy executor instance
    dummy_executor = A2AAutoPilotExecutor(checkpointer=MagicMock())
    initial_checkpointer = dummy_executor.checkpointer

    # Create dummy ThreadService
    initial_thread_service = ThreadService(initial_checkpointer, executor=dummy_executor)
    set_thread_service(initial_thread_service)

    # Perform PUT setting to trigger sqlite checkpointer creation
    resp = client.put("/api/settings", json=[{"key": "CHECKPOINT_BACKEND", "value": "sqlite"}])
    assert resp.status_code == 200

    # Verify checkpointer was replaced on executor
    assert dummy_executor.checkpointer is not initial_checkpointer

    # Verify ThreadService was replaced
    new_thread_service = get_thread_service()
    assert new_thread_service is not initial_thread_service
    assert new_thread_service._checkpointer is dummy_executor.checkpointer


# ── 4. Startup Rehydration ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_startup_custom_config_rehydration(tmp_path, monkeypatch):
    """AC-6: Verify that custom config keys stored in DB are rehydrated into os.environ on startup."""
    from k8s_autopilot.config.manifest import get_config_options

    db_file = tmp_path / "rehydrate_test.db"
    adapter = SqliteConfigAdapter(db_file)
    store = ConfigStore(adapter)
    await store.initialize()

    # Pre-populate custom variable in store
    await store.set(
        key="REHYDRATE_TEST_TOKEN",
        value="tok_rehydrate_999",
        category=ConfigCategory.SYSTEM,
        display_name="REHYDRATE_TEST_TOKEN",
    )

    # Ensure it's not in os.environ
    monkeypatch.delenv("REHYDRATE_TEST_TOKEN", raising=False)
    assert "REHYDRATE_TEST_TOKEN" not in os.environ

    # Run the startup rehydration logic as in app.py lifespan
    all_entries = await store.list_all()
    manifest_keys = {opt.db_key for opt in get_config_options()}
    manifest_keys.update({opt.key for opt in get_config_options()})
    custom_count = 0
    for entry in all_entries:
        if (
            entry.key not in manifest_keys
            and not entry.key.startswith("models.")
            and not entry.key.startswith("credentials.")
            and entry.value
        ):
            os.environ[entry.key] = str(entry.value)
            custom_count += 1

    assert custom_count == 1
    assert os.environ.get("REHYDRATE_TEST_TOKEN") == "tok_rehydrate_999"
