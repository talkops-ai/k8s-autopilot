"""Unit tests for PluginSkillsMiddleware and skill discovery."""

from __future__ import annotations

from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend

from k8s_autopilot.middleware.skills import PluginSkillsMiddleware, discover_skill_dirs


class TestPluginSkillsMiddleware:
    def test_discover_skill_dirs(self, tmp_path: Path) -> None:
        skill_1 = tmp_path / "skill_1"
        skill_1.mkdir()
        (skill_1 / "SKILL.md").write_text("---\nname: test_skill\n---\n")

        backend = FilesystemBackend(virtual_mode=False)
        discovered = discover_skill_dirs(backend, str(tmp_path))
        assert len(discovered) == 1
        assert str(skill_1) in discovered[0][0]

    def test_is_skill_allowed(self, tmp_path: Path) -> None:
        mw = PluginSkillsMiddleware(
            backend=FilesystemBackend(virtual_mode=False),
            sources=[],
            allowed_skills=["k8s_*", "helm"],
        )
        assert mw._is_skill_allowed("k8s_debug")
        assert mw._is_skill_allowed("helm")
        assert not mw._is_skill_allowed("python_eval")
