"""Unit tests for Settings and Model API endpoints."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.api.settings_routes import create_settings_routes, set_config_store
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore


@pytest.fixture
async def app_client(tmp_path):
    adapter = SqliteConfigAdapter(tmp_path / "test_api.db")
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    routes = create_settings_routes()
    app = Starlette(routes=routes)
    return TestClient(app)


def test_list_models_endpoint(app_client):
    resp = app_client.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_model" in data
    assert "models_by_provider" in data
    assert "google_genai" in data["models_by_provider"]
    assert "anthropic" in data["models_by_provider"]


def test_get_model_effort_endpoint(app_client):
    resp = app_client.get("/api/models/effort?model=google_genai:gemini-3.7-flash")
    assert resp.status_code == 200
    data = resp.json()
    assert "supported_efforts" in data
    assert "high" in data["supported_efforts"]


def test_select_model_endpoint(app_client):
    resp = app_client.post(
        "/api/models/select",
        json={"model": "anthropic:claude-3-7-sonnet", "effort": "high"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["model"] == "anthropic:claude-3-7-sonnet"
    assert data["effort"] == "high"

    # Verify model is active
    models_resp = app_client.get("/api/models")
    assert models_resp.json()["current_model"] == "anthropic:claude-3-7-sonnet"


def test_get_and_put_settings(app_client):
    # GET settings
    resp = app_client.get("/api/settings")
    assert resp.status_code == 200
    settings = resp.json()
    assert isinstance(settings, list)
    assert len(settings) > 0

    # PUT setting
    put_resp = app_client.put(
        "/api/settings",
        json=[{"key": "k8s.namespace", "value": "production"}],
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["success"] is True

    # GET single setting
    single_resp = app_client.get("/api/settings/k8s.namespace")
    assert single_resp.status_code == 200
    assert single_resp.json()["value"] == "production"


def test_mcp_servers_api(app_client):
    server = {
        "name": "api-test-mcp",
        "transport": "sse",
        "url": "http://localhost:8080/sse",
        "enabled": True,
        "trusted": True,
    }
    put_resp = app_client.put("/api/mcp-servers", json=[server])
    assert put_resp.status_code == 200

    list_resp = app_client.get("/api/mcp-servers")
    assert list_resp.status_code == 200
    servers = list_resp.json()
    assert len(servers) == 1
    assert servers[0]["name"] == "api-test-mcp"

    del_resp = app_client.delete("/api/mcp-servers/api-test-mcp")
    assert del_resp.status_code == 200


def test_get_setting_reveal_secret(app_client):
    # Save a secret key
    put_resp = app_client.put(
        "/api/settings",
        json=[{"key": "credentials.openai", "value": "sk-secret-key-12345"}],
    )
    assert put_resp.status_code == 200

    # Normal GET single setting returns masked value
    get_resp = app_client.get("/api/settings/credentials.openai")
    assert get_resp.status_code == 200
    assert get_resp.json()["value"] == "******"
    assert get_resp.json()["is_sensitive"] is True

    # GET with ?reveal=true returns unredacted plaintext value
    reveal_resp = app_client.get("/api/settings/credentials.openai?reveal=true")
    assert reveal_resp.status_code == 200
    assert reveal_resp.json()["value"] == "sk-secret-key-12345"

    # GET list still returns masked value
    list_resp = app_client.get("/api/settings")
    assert list_resp.status_code == 200
    openai_setting = next(s for s in list_resp.json() if s["key"] == "credentials.openai")
    assert openai_setting["value"] == "******"



def test_plugins_and_skills_api(app_client):
    # Plugin
    plugin = {
        "plugin_id": "test-plugin@local",
        "name": "test-plugin",
        "marketplace": "local",
        "enabled": True,
    }
    p_resp = app_client.post("/api/plugins", json=plugin)
    assert p_resp.status_code == 200

    # Skill
    skill = {
        "name": "test-skill",
        "description": "A skill",
        "enabled": True,
    }
    s_resp = app_client.post("/api/skills", json=skill)
    assert s_resp.status_code == 200


def test_marketplaces_and_plugin_lifecycle_api(app_client, tmp_path):
    # 1. Create a mock marketplace directory
    m_dir = tmp_path / "api_mock_marketplace"
    m_dir.mkdir(parents=True, exist_ok=True)
    p_dir = m_dir / "plugins" / "argocd-checker"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "plugin.json").write_text(
        '{"name": "argocd-checker", "version": "1.0.0", "displayName": "ArgoCD Checker", "description": "Checks ArgoCD apps", "skills": ["./skills"]}',
        encoding="utf-8",
    )
    (p_dir / "skills").mkdir(parents=True, exist_ok=True)
    (p_dir / "skills" / "SKILL.md").write_text("# ArgoCD skill", encoding="utf-8")

    (m_dir / "marketplace.json").write_text(
        f'{{"name": "test-api-marketplace", "plugins": [{{"name": "argocd-checker", "displayName": "ArgoCD Checker", "description": "Checks ArgoCD apps", "source": "./plugins/argocd-checker"}}]}}',
        encoding="utf-8",
    )

    # 2. Add Marketplace POST /api/marketplaces
    add_resp = app_client.post("/api/marketplaces", json={"source": str(m_dir)})
    assert add_resp.status_code == 200
    assert add_resp.json()["name"] == "test-api-marketplace"
    assert add_resp.json()["plugin_count"] == 1

    # 3. GET /api/marketplaces
    m_list_resp = app_client.get("/api/marketplaces")
    assert m_list_resp.status_code == 200
    marketplaces = m_list_resp.json()
    assert len(marketplaces) == 1
    assert marketplaces[0]["name"] == "test-api-marketplace"

    # 4. GET /api/plugins/discover
    disc_resp = app_client.get("/api/plugins/discover")
    assert disc_resp.status_code == 200
    avail = disc_resp.json()
    assert len(avail) == 1
    assert avail[0]["plugin_id"] == "argocd-checker@test-api-marketplace"
    assert avail[0]["installed"] is False

    # 5. POST /api/plugins/install (centralized install without scope)
    inst_resp = app_client.post(
        "/api/plugins/install",
        json={"plugin_id": "argocd-checker@test-api-marketplace"},
    )
    assert inst_resp.status_code == 200
    assert inst_resp.json()["plugin_id"] == "argocd-checker@test-api-marketplace"

    # 5b. GET /api/plugins/discover (should exclude installed plugin)
    disc_after_inst = app_client.get("/api/plugins/discover")
    assert disc_after_inst.status_code == 200
    assert len(disc_after_inst.json()) == 0

    # 5c. GET /api/plugins/discover?include_installed=true
    disc_incl = app_client.get("/api/plugins/discover?include_installed=true")
    assert disc_incl.status_code == 200
    assert len(disc_incl.json()) == 1
    assert disc_incl.json()[0]["installed"] is True

    # 6. GET /api/plugins/installed
    installed_resp = app_client.get("/api/plugins/installed")
    assert installed_resp.status_code == 200
    installed = installed_resp.json()
    assert any(p["plugin_id"] == "argocd-checker@test-api-marketplace" for p in installed)

    # 7. POST /api/plugins/{id}/disable
    dis_resp = app_client.post("/api/plugins/argocd-checker@test-api-marketplace/disable")
    assert dis_resp.status_code == 200
    assert dis_resp.json()["success"] is True

    # 8. POST /api/plugins/{id}/enable
    en_resp = app_client.post("/api/plugins/argocd-checker@test-api-marketplace/enable")
    assert en_resp.status_code == 200
    assert en_resp.json()["success"] is True

    # 9. POST /api/plugins/{id}/uninstall
    uninst_resp = app_client.post("/api/plugins/argocd-checker@test-api-marketplace/uninstall")
    assert uninst_resp.status_code == 200
    assert uninst_resp.json()["success"] is True

    # 9b. GET /api/plugins/discover (plugin should reappear in discover)
    disc_after_uninst = app_client.get("/api/plugins/discover")
    assert disc_after_uninst.status_code == 200
    assert len(disc_after_uninst.json()) == 1
    assert disc_after_uninst.json()[0]["plugin_id"] == "argocd-checker@test-api-marketplace"

    # 10. DELETE /api/marketplaces/{name}
    del_m_resp = app_client.delete("/api/marketplaces/test-api-marketplace")
    assert del_m_resp.status_code == 200
    assert del_m_resp.json()["success"] is True



def test_generate_argocd_token_missing_fields(app_client, monkeypatch):
    monkeypatch.delenv("ARGOCD_SERVER_URL", raising=False)
    # Missing server_url
    resp1 = app_client.post("/api/settings/generate-argocd-token", json={"username": "admin", "password": "pw"})
    assert resp1.status_code == 400
    assert "Missing ArgoCD Server URL" in resp1.json()["error"]

    # Missing username
    resp2 = app_client.post("/api/settings/generate-argocd-token", json={"server_url": "https://argocd.local", "password": "pw"})
    assert resp2.status_code == 400
    assert "Missing ArgoCD Username" in resp2.json()["error"]

    # Missing password
    resp3 = app_client.post("/api/settings/generate-argocd-token", json={"server_url": "https://argocd.local", "username": "admin"})
    assert resp3.status_code == 400
    assert "Missing ArgoCD Password" in resp3.json()["error"]


def test_generate_argocd_token_success(app_client, monkeypatch):
    import httpx

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data
            self.text = "OK"

        def json(self):
            return self._json_data

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def post(self, url, json=None, **kwargs):
            if "api/v1/session" in url:
                if json.get("password") == "valid-pass":
                    return MockResponse(200, {"token": "jwt-token-12345"})
                return MockResponse(401, {"error": "Invalid credentials"})
            return MockResponse(404, {})

        async def get(self, url, headers=None, **kwargs):
            if "api/v1/applications" in url and headers.get("Authorization") == "Bearer jwt-token-12345":
                return MockResponse(200, {"items": []})
            return MockResponse(401, {"error": "Unauthorized"})

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    # 1. Successful token generation
    resp = app_client.post(
        "/api/settings/generate-argocd-token",
        json={
            "server_url": "https://argocd.example.com",
            "username": "admin",
            "password": "valid-pass",
            "insecure": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["token"] == "jwt-token-12345"

    # Verify setting was saved
    setting_resp = app_client.get("/api/settings/credentials.argocd")
    assert setting_resp.status_code == 200

    # 2. Failed authentication
    fail_resp = app_client.post(
        "/api/settings/generate-argocd-token",
        json={
            "server_url": "https://argocd.example.com",
            "username": "admin",
            "password": "wrong-pass",
            "insecure": True,
        },
    )
    assert fail_resp.status_code == 400
    assert fail_resp.json()["success"] is False
    assert "Invalid credentials" in fail_resp.json()["error"]


def test_integration_argocd(app_client, monkeypatch):
    import httpx

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def get(self, url, headers=None, **kwargs):
            class Resp:
                status_code = 200
                text = "OK"
            return Resp()

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    # Test success with payload
    resp = app_client.post(
        "/api/settings/test-integration",
        json={
            "type": "argocd",
            "payload": {
                "ARGOCD_SERVER_URL": "https://argocd.example.com",
                "ARGOCD_AUTH_TOKEN": "my-test-token",
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
