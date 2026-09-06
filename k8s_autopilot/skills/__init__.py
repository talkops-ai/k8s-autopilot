"""Skills module for K8s Autopilot.

Provides dynamic skill loading, registration, containment security, trust management,
and prompt invocation enveloping with Database as Source of Truth.
"""

from __future__ import annotations

from k8s_autopilot.skills.commands import list_skills_command, trust_skill_command
from k8s_autopilot.skills.invocation import (
    STATIC_SKILL_ALIASES,
    SkillInvocationEnvelope,
    build_skill_invocation_envelope,
    parse_skill_command,
)
from k8s_autopilot.skills.loader import (
    ExtendedSkillMetadata,
    list_skills,
    load_skill_content,
)
from k8s_autopilot.skills.registry import (
    SkillMetadata,
    SkillRegistry,
    SkillSource,
    get_skill_registry,
)
from k8s_autopilot.skills.trust import SkillTrustStore

__all__ = [
    "ExtendedSkillMetadata",
    "STATIC_SKILL_ALIASES",
    "SkillInvocationEnvelope",
    "SkillMetadata",
    "SkillRegistry",
    "SkillSource",
    "SkillTrustStore",
    "build_skill_invocation_envelope",
    "get_skill_registry",
    "list_skills",
    "list_skills_command",
    "load_skill_content",
    "parse_skill_command",
    "trust_skill_command",
]
