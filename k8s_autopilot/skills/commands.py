"""CLI and API commands for managing K8s Autopilot skills.

Ported from ``reference/opscode/src/opscode/skills/commands.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.skills.loader import list_skills
from k8s_autopilot.skills.trust import SkillTrustStore


def list_skills_command() -> list[dict[str, Any]]:
    """List all discovered skills and their trust status."""
    settings = get_settings()
    discovered = list_skills(project_root=settings.project_root)
    store = SkillTrustStore()

    results = []
    for skill in discovered:
        path_str = skill.get("path", "")
        name_str = skill.get("name", "")
        source_str = skill.get("source", "")
        path = Path(path_str)
        trusted = store.is_trusted(name_str, path)
        results.append({
            "name": name_str,
            "description": skill.get("description", ""),
            "source": source_str,
            "plugin_id": skill.get("plugin_id"),
            "path": str(path),
            "trusted": "Yes" if trusted else "No",
        })
    return results


def trust_skill_command(name: str, path_str: str) -> bool:
    """Trust a skill directory."""
    path = Path(path_str)
    if not path.exists():
        return False
    store = SkillTrustStore()
    store.trust_skill(name, path)
    return True
