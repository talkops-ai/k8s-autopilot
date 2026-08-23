"""
Unit tests for SkillRegistry dynamic discovery and virtual path resolution (dcode-aligned).

Verifies that:
- Standard skills and plugin skills are discovered from plugins/ directory structure.
- Virtual paths are mapped correctly (e.g., /skills/helm-operator/helm-operation).
- Seeding maps virtual paths correctly inside virtual filesystem backend.
"""

from pathlib import Path
import pytest

from k8s_autopilot.core.skills.registry import get_skill_registry


def test_discover_plugin_skills():
    """Verify that coordinator and agent skills are discovered from plugins/."""
    registry = get_skill_registry()
    registry.discover_skills(force=True)

    # 1. Coordinator skill should be discovered
    coordinator_skill = registry.get_skill("helm-operator-coordinator")
    if not coordinator_skill:
        # Fallback search by virtual path
        coordinator_skill = next(
            (s for s in registry.list_skills() if s["virtual_path"] == "/skills/helm-operator/coordinator"),
            None
        )
    assert coordinator_skill is not None
    assert coordinator_skill["domain"] == "helm-operator"
    assert coordinator_skill["virtual_path"] == "/skills/helm-operator/coordinator"

    # 2. Subagent skill should be discovered
    operation_skill = registry.get_skill("helm-operation")
    if not operation_skill:
        operation_skill = next(
            (s for s in registry.list_skills() if s["virtual_path"] == "/skills/helm-operator/helm-operation"),
            None
        )
    assert operation_skill is not None
    assert operation_skill["domain"] == "helm-operator"
    assert operation_skill["virtual_path"] == "/skills/helm-operator/helm-operation"


def test_seed_skills_files():
    """Verify that seed_skills_files maps physical files to the correct virtual path layout."""
    registry = get_skill_registry()
    registry.discover_skills(force=True)

    # Seed the helm-operation skill path
    files = registry.seed_skills_files(["/skills/helm-operator/helm-operation"])

    # Verify we seeded the SKILL.md file at the correct virtual path prefix
    vpath_skill = "/skills/helm-operator/helm-operation/SKILL.md"
    assert vpath_skill in files
    assert files[vpath_skill]["content"] is not None

    # Verify we seeded troubleshooting and reference workflow files
    vpath_trouble = "/skills/helm-operator/helm-operation/TROUBLESHOOTING.md"
    assert vpath_trouble in files
