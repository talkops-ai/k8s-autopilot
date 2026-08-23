"""Skills discovery and registry module for k8s-autopilot."""

from k8s_autopilot.core.skills.registry import (
    SkillMetadata,
    SkillRegistry,
    get_skill_registry,
)

__all__ = ["SkillMetadata", "SkillRegistry", "get_skill_registry"]
