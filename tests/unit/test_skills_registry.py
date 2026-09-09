"""Unit tests for Skills Registry (Phase 13)."""

from __future__ import annotations

from pathlib import Path

import pytest


class TestSkillRegistry:
    """Tests for SkillRegistry and multi-tier discovery."""

    def test_singleton(self) -> None:
        from k8s_autopilot.skills.registry import get_skill_registry

        r1 = get_skill_registry()
        r2 = get_skill_registry()
        assert r1 is r2

    def test_register_and_get(self) -> None:
        from k8s_autopilot.skills.registry import SkillRegistry, SkillSource

        registry = SkillRegistry()
        skill = SkillSource(
            name="kubernetes",
            path=Path("/fake/skills/kubernetes"),
            tier="builtin",
            description="K8s operations",
        )
        registry.register(skill)
        assert registry.get("kubernetes") is skill

    def test_get_unknown_returns_none(self) -> None:
        from k8s_autopilot.skills.registry import SkillRegistry

        registry = SkillRegistry()
        assert registry.get("nonexistent") is None

    def test_list_skills(self) -> None:
        from k8s_autopilot.skills.registry import SkillRegistry, SkillSource

        registry = SkillRegistry()
        registry.register(SkillSource("beta", Path("/b"), "builtin"))
        registry.register(SkillSource("alpha", Path("/a"), "user"))
        skills = registry.list_skills()
        assert [s.name for s in skills] == ["alpha", "beta"]

    def test_list_skills_by_tier(self) -> None:
        from k8s_autopilot.skills.registry import SkillRegistry, SkillSource

        registry = SkillRegistry()
        registry.register(SkillSource("k8s", Path("/k"), "builtin"))
        registry.register(SkillSource("custom", Path("/c"), "user"))
        assert len(registry.list_skills(tier="builtin")) == 1
        assert len(registry.list_skills(tier="user")) == 1
        assert len(registry.list_skills(tier="project")) == 0

    def test_discover_from_filesystem(self, tmp_path: Path) -> None:
        from k8s_autopilot.skills.registry import SkillRegistry

        # Create a valid skill directory
        skill_dir = tmp_path / "test_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test_skill\ndescription: A test skill\n---\n# Test\n"
        )

        registry = SkillRegistry()
        count = registry.discover(builtin_dir=tmp_path)
        assert count == 1
        skill = registry.get("test_skill")
        assert skill is not None
        assert skill.tier == "builtin"

    def test_discover_ignores_non_skill_dirs(self, tmp_path: Path) -> None:
        from k8s_autopilot.skills.registry import SkillRegistry

        # Create a directory without SKILL.md
        (tmp_path / "not_a_skill").mkdir()
        # Create a file (not a dir)
        (tmp_path / "some_file.txt").write_text("hello")

        registry = SkillRegistry()
        assert registry.discover(builtin_dir=tmp_path) == 0


class TestSkillFrontmatterParsing:
    """Tests for YAML frontmatter parsing."""

    def test_parse_description(self, tmp_path: Path) -> None:
        from k8s_autopilot.skills.registry import _parse_skill_frontmatter

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            '---\nname: test\ndescription: "My skill"\n---\n# Body\n'
        )
        desc, tags = _parse_skill_frontmatter(skill_md)
        assert desc == "My skill"

    def test_parse_no_frontmatter(self, tmp_path: Path) -> None:
        from k8s_autopilot.skills.registry import _parse_skill_frontmatter

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# Just a markdown file\n")
        desc, tags = _parse_skill_frontmatter(skill_md)
        assert desc == ""
        assert tags == ()

    def test_parse_missing_file(self, tmp_path: Path) -> None:
        from k8s_autopilot.skills.registry import _parse_skill_frontmatter

        missing = tmp_path / "nonexistent.md"
        desc, tags = _parse_skill_frontmatter(missing)
        assert desc == ""
        assert tags == ()


class TestBuiltInSkillsDirectory:
    """Tests for the built-in skills directory."""

    def test_directory_exists(self) -> None:
        built_in_dir = (
            Path(__file__).parent.parent.parent
            / "k8s_autopilot"
            / "built_in_skills"
        )
        assert built_in_dir.is_dir()

    def test_no_redundant_built_in_skills(self) -> None:
        built_in_dir = (
            Path(__file__).parent.parent.parent
            / "k8s_autopilot"
            / "built_in_skills"
        )
        assert not (built_in_dir / "kubernetes").exists()
        assert not (built_in_dir / "remember").exists()

