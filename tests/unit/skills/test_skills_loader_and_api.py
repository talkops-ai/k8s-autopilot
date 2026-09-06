"""Unit tests for skills loader discovery and FastAPI /api/skills endpoints."""

from __future__ import annotations

from pathlib import Path
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.api.settings_routes import create_settings_routes
from k8s_autopilot.skills.loader import (
    get_skill_by_name,
    get_skill_content_by_name,
    list_skills,
    load_skill_content,
)


class TestSkillsLoader:
    """Tests for list_skills and skill content loading."""

    def test_list_skills_built_in(self) -> None:
        skills = list_skills(include_plugins=False, include_subagents=False)
        names = [s["name"] for s in skills]
        assert "kubernetes" in names
        assert "remember" in names

        k8s_skill = next(s for s in skills if s["name"] == "kubernetes")
        assert k8s_skill["scope"] == "BUILT-IN"
        assert k8s_skill["source"] == "built-in"
        assert "Kubernetes" in k8s_skill["description"]
        assert k8s_skill["license"] == "MIT"
        assert "kubernetes" in k8s_skill["tags"]

    def test_get_skill_by_name(self) -> None:
        skill = get_skill_by_name("kubernetes")
        assert skill is not None
        assert skill["name"] == "kubernetes"
        assert Path(skill["path"]).name == "SKILL.md"

    def test_get_skill_content_by_name(self) -> None:
        skill, content = get_skill_content_by_name("kubernetes")
        assert skill is not None
        assert content is not None
        assert "name: kubernetes" in content
        assert "# Kubernetes" in content

    def test_load_skill_content_ssrf_safety(self, tmp_path: Path) -> None:
        outside_file = tmp_path / "outside" / "SKILL.md"
        outside_file.parent.mkdir(parents=True)
        outside_file.write_text("---\nname: evil\n---\nEvil content")

        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir(parents=True)

        with pytest.raises(PermissionError, match="SSRF prevention"):
            load_skill_content(str(outside_file), allowed_roots=[allowed_root])


class TestSkillsApiRoutes:
    """Tests for /api/skills HTTP endpoints."""

    @pytest.fixture
    def client(self) -> TestClient:
        app = Starlette(routes=create_settings_routes())
        return TestClient(app)

    def test_get_skills_list(self, client: TestClient) -> None:
        response = client.get("/api/skills")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        names = [s["name"] for s in data]
        assert "kubernetes" in names
        assert "remember" in names

        k8s_item = next(s for s in data if s["name"] == "kubernetes")
        assert k8s_item["scope"] == "BUILT-IN"
        assert "location" in k8s_item
        assert "path" in k8s_item

    def test_get_skill_detail(self, client: TestClient) -> None:
        response = client.get("/api/skills/kubernetes")
        assert response.status_code == 200
        data = response.json()
        assert "skill" in data
        assert "content" in data
        assert data["skill"]["name"] == "kubernetes"
        assert "# Kubernetes" in data["content"]

    def test_get_skill_content_endpoint(self, client: TestClient) -> None:
        response = client.get("/api/skills/kubernetes/content")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "kubernetes"
        assert "content" in data
        assert data["scope"] == "BUILT-IN"

    def test_get_nonexistent_skill(self, client: TestClient) -> None:
        response = client.get("/api/skills/nonexistent-skill-xyz")
        assert response.status_code == 404
