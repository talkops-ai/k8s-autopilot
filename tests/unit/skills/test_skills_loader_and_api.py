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

    def test_list_skills_built_in_empty(self) -> None:
        """Verify built-in skills directory contains no redundant skills."""
        skills = list_skills(include_plugins=False, include_subagents=False)
        names = [s["name"] for s in skills]
        assert "kubernetes" not in names
        assert "remember" not in names

    def test_list_skills_from_directory(self, tmp_path: Path) -> None:
        """Verify discovering and parsing skills from a custom directory."""
        skill_dir = tmp_path / "skills" / "custom-ops"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: custom-ops\n"
            "description: Operational procedures for custom services\n"
            "license: Apache-2.0\n"
            "tags:\n"
            "  - operations\n"
            "  - sre\n"
            "---\n"
            "# Custom Ops\n"
            "Execute safe operations.\n"
        )
        empty_builtin = tmp_path / "empty_builtin"
        empty_builtin.mkdir()

        skills = list_skills(
            built_in_skills_dir=empty_builtin,
            project_skills_dir=tmp_path / "skills",
            include_plugins=False,
            include_subagents=False,
        )
        names = [s["name"] for s in skills]
        assert "custom-ops" in names

        skill = next(s for s in skills if s["name"] == "custom-ops")
        assert skill.get("scope") == "PROJECT"
        assert skill.get("license") == "Apache-2.0"
        tags = skill.get("tags") or []
        assert "operations" in tags
        assert "sre" in tags
        assert "Operational procedures" in skill["description"]

    def test_load_skill_content(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# My Skill\nContent here")
        content = load_skill_content(str(skill_file))
        assert content is not None
        assert "# My Skill" in content

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

    def test_get_skills_list_empty_builtins(self, client: TestClient) -> None:
        response = client.get("/api/skills")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        names = [s["name"] for s in data]
        assert "kubernetes" not in names
        assert "remember" not in names

    def test_get_skill_detail_and_content(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_skill = {
            "name": "canary-rollback",
            "description": "Rollback canary deployments",
            "path": "/fake/path/SKILL.md",
            "scope": "PROJECT",
            "source": "project",
        }
        monkeypatch.setattr(
            "k8s_autopilot.skills.loader.get_skill_content_by_name",
            lambda name, **kwargs: (fake_skill, "# Canary Rollback\nSteps to rollback")
            if name == "canary-rollback"
            else (None, None),
        )

        # GET /api/skills/canary-rollback
        response = client.get("/api/skills/canary-rollback")
        assert response.status_code == 200
        data = response.json()
        assert data["skill"]["name"] == "canary-rollback"
        assert "# Canary Rollback" in data["content"]

        # GET /api/skills/canary-rollback/content
        response_content = client.get("/api/skills/canary-rollback/content")
        assert response_content.status_code == 200
        content_data = response_content.json()
        assert content_data["name"] == "canary-rollback"
        assert content_data["content"] == "# Canary Rollback\nSteps to rollback"
        assert content_data["scope"] == "PROJECT"

    def test_get_nonexistent_skill(self, client: TestClient) -> None:
        response = client.get("/api/skills/nonexistent-skill-xyz")
        assert response.status_code == 404
