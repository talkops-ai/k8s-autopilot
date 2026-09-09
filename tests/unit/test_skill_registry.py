"""Unit tests for multi-tier SkillRegistry (Phase 13)."""

from __future__ import annotations

from pathlib import Path
import pytest

from k8s_autopilot.skills.registry import SkillRegistry, SkillSource, get_skill_registry


def test_skill_registry_singleton() -> None:
    """Verify get_skill_registry returns a singleton instance."""
    reg1 = get_skill_registry()
    reg2 = SkillRegistry.get_instance()
    assert reg1 is reg2


def test_register_and_get_skill(tmp_path: Path) -> None:
    """Verify manual registration and lookup in SkillRegistry."""
    registry = SkillRegistry()
    skill_path = tmp_path / "k8s-diagnostics"
    skill_path.mkdir()

    source = SkillSource(
        name="k8s-diagnostics",
        path=skill_path,
        tier="builtin",
        description="Kubernetes diagnostics skill",
        tags=("k8s", "debug"),
    )
    registry.register(source)

    retrieved = registry.get("k8s-diagnostics")
    assert retrieved is not None
    assert retrieved.name == "k8s-diagnostics"
    assert retrieved.tier == "builtin"
    assert "debug" in retrieved.tags

    assert len(registry.list_skills(tier="builtin")) == 1
    assert len(registry.list_skills(tier="user")) == 0


def test_discover_skills_from_directory(tmp_path: Path) -> None:
    """Verify filesystem discovery of skills with frontmatter."""
    registry = SkillRegistry()

    skill_dir = tmp_path / "helm-deploy"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "description: Automated Helm deployments\n"
        "tags:\n"
        "  - helm\n"
        "  - deploy\n"
        "---\n\n"
        "# Helm Deploy Skill\n",
        encoding="utf-8",
    )

    count = registry.discover(builtin_dir=tmp_path)
    assert count == 1

    skill = registry.get("helm-deploy")
    assert skill is not None
    assert skill.description == "Automated Helm deployments"
    assert "helm" in skill.tags
