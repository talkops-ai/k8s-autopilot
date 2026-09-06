"""Adapter from discovered plugins to K8s Autopilot skill sources."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from k8s_autopilot.plugins.models import PluginInstance

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

SkillPath: TypeAlias = str
SkillLabel: TypeAlias = str
SkillNamespace: TypeAlias = str
PluginSkillSource: TypeAlias = tuple[SkillPath, SkillLabel, SkillNamespace]


def namespaced_skill_name(
    namespace: SkillNamespace,
    name: str,
    subfolders: tuple[str, ...] = (),
) -> str:
    """Qualify a skill name under its plugin namespace."""
    return ":".join((namespace, *subfolders, name)).lower()


def plugin_skill_sources(
    plugins: tuple[PluginInstance, ...] | list[PluginInstance],
) -> list[PluginSkillSource]:
    """Return skill source tuples for plugin skills."""
    sources: list[PluginSkillSource] = []
    for plugin in plugins:
        for path in plugin.inventory.skills:
            source_path = path.parent if path.name == "SKILL.md" else path
            try:
                if not source_path.exists():
                    continue
            except OSError:
                logger.warning("Could not inspect plugin skill path %s", source_path)
                continue
            sources.append(
                (
                    str(source_path),
                    f"Plugin: {plugin.plugin_id}",
                    plugin.plugin_id,
                )
            )
    return sources


def plugin_skill_roots(plugins: tuple[PluginInstance, ...] | list[PluginInstance]) -> list[Path]:
    """Return plugin skill roots for skill-content containment checks."""
    roots: list[Path] = []
    for plugin in plugins:
        roots.extend(
            path.parent if path.name == "SKILL.md" else path
            for path in plugin.inventory.skills
        )
    return roots
