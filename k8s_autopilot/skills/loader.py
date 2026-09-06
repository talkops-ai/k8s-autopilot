"""Skill loader for K8s Autopilot — built-in skills, plugin skills, user skills, and project skills.

Supports:
- Built-in skills (packaged in ``built_in_skills/``)
- User skills (``~/.k8s_autopilot/skills/`` and ``~/.agents/skills/``)
- Project skills (``<project_root>/.k8s_autopilot/skills/``, ``.agents/skills/``, and ``skills/``)
- Non-agent plugin skills (installed marketplace and cache plugins)
- Subagent skills (optional when ``include_subagents=True``)
- SSRF containment-safe skill content loading
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Required, Sequence, TypedDict, cast

import yaml
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import (
    SkillMetadata,
    _list_skills as list_skills_from_backend,
)

from k8s_autopilot.config.paths import DATA_DIR

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class ExtendedSkillMetadata(TypedDict, total=False):
    """Extended skill metadata with source, location, preview, and frontmatter tracking."""

    name: Required[str]
    description: Required[str]
    path: Required[str]
    location: str
    virtual_path: str
    source: Required[Literal["built-in", "user", "project", "plugin", "subagent"]]
    scope: str
    plugin_id: str | None
    plugin_type: str | None
    license: str | None
    compatibility: str | None
    tags: list[str]
    domain: str
    preview: str
    enabled: bool
    frontmatter: dict[str, Any]
    system_prompt: str


def _parse_skill_file(skill_md: Path) -> dict[str, Any]:
    """Parse SKILL.md for frontmatter and markdown preview."""
    meta: dict[str, Any] = {
        "description": "",
        "license": None,
        "compatibility": None,
        "tags": [],
        "domain": "",
        "preview": "",
        "frontmatter": {},
    }
    if not skill_md.is_file():
        return meta

    try:
        content = skill_md.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
        if match:
            fm_text, body_text = match.group(1), match.group(2)
            fm = yaml.safe_load(fm_text)
            if isinstance(fm, dict):
                meta["frontmatter"] = fm
                meta["description"] = str(fm.get("description") or "").strip()
                meta["license"] = fm.get("license")
                meta["compatibility"] = fm.get("compatibility")
                raw_tags = fm.get("tags") or []
                meta["tags"] = (
                    [str(t) for t in raw_tags]
                    if isinstance(raw_tags, (list, tuple))
                    else []
                )
                meta["domain"] = str(fm.get("domain") or "")

            body_clean = body_text.strip()
            body_lines = [
                line.strip()
                for line in body_clean.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if body_lines:
                meta["preview"] = "\n".join(body_lines[:4])
            else:
                meta["preview"] = meta["description"]
        else:
            lines = [
                l.strip()
                for l in content.splitlines()
                if l.strip() and not l.strip().startswith("#")
            ]
            meta["preview"] = "\n".join(lines[:4]) if lines else ""
    except Exception as exc:
        logger.debug("Failed parsing skill metadata from %s: %s", skill_md, exc)

    return meta


def _discover_plugin_skills(plugins_dir: Path) -> list[tuple[Path, str, str, str]]:
    """Discover skills from Claude and Codex plugins."""
    discovered: list[tuple[Path, str, str, str]] = []
    if not plugins_dir.is_dir():
        return discovered

    for p in sorted(plugins_dir.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue

        p_id = p.name
        plugin_type = "custom"

        # Check Claude plugin manifest
        claude_manifest = p / "plugin.json"
        codex_manifest = p / "ai-plugin.json"

        if claude_manifest.is_file():
            plugin_type = "claude"
            try:
                with open(claude_manifest, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    p_id = data.get("name", p.name)
            except Exception:
                pass
        elif codex_manifest.is_file():
            plugin_type = "codex"
            try:
                with open(codex_manifest, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    p_id = data.get("name_for_model") or data.get("name_for_human", p.name)
            except Exception:
                pass

        skills_dir = p / "skills"
        if skills_dir.is_dir():
            discovered.append((skills_dir, "plugin", p_id, plugin_type))

    return discovered


def list_skills(
    *,
    built_in_skills_dir: Path | None = None,
    user_skills_dir: Path | None = None,
    project_skills_dir: Path | None = None,
    include_plugins: bool = True,
    include_subagents: bool = False,
    project_root: Path | None = None,
    store: Any = None,
) -> list[ExtendedSkillMetadata]:
    """List skills from built-in, user, project, and plugin directories.

    Args:
        built_in_skills_dir: Optional override for built-in skills directory.
        user_skills_dir: Optional override for user skills directory.
        project_skills_dir: Optional override for project skills directory.
        include_plugins: Whether to include discovered marketplace/cache plugin skills.
        include_subagents: Whether to include subagent-specific bundled skills.
        project_root: Effective project root directory.
        store: Optional ConfigStore reference.

    Returns:
        List of ExtendedSkillMetadata items attached to the agent.
    """
    all_skills: dict[str, ExtendedSkillMetadata] = {}

    sources: list[
        tuple[
            Path | None,
            Literal["built-in", "user", "project", "plugin", "subagent"],
            str,
            str,
            str,
        ]
    ] = []

    # 1. Tier 1: Built-in skills
    bi_dir = built_in_skills_dir or (Path(__file__).parent.parent / "built_in_skills")
    sources.append((bi_dir, "built-in", "", "builtin", "BUILT-IN"))

    # 2. Tier 2: User skills (~/.k8s_autopilot/skills and ~/.agents/skills)
    user_dir = user_skills_dir or (DATA_DIR / "skills")
    if user_dir.is_dir():
        sources.append((user_dir, "user", "", "user", "USER"))
    agents_user_dir = Path.home() / ".agents" / "skills"
    if agents_user_dir.is_dir():
        sources.append((agents_user_dir, "user", "", "user", "USER"))

    # 3. Tier 3: Project skills (<root>/.k8s_autopilot/skills, <root>/.agents/skills, <root>/skills)
    effective_root = project_root or Path.cwd()
    if project_skills_dir and project_skills_dir.is_dir():
        sources.append((project_skills_dir, "project", "", "project", "PROJECT"))
    else:
        for p_cand in [
            effective_root / ".k8s_autopilot" / "skills",
            effective_root / ".agents" / "skills",
            effective_root / "skills",
        ]:
            if p_cand.is_dir():
                sources.append((p_cand, "project", "", "project", "PROJECT"))

    # 4. Tier 4: Plugin skills (Installed marketplace plugins and local non-agent plugins)
    if include_plugins:
        # Local project plugins (project_root/plugins)
        proj_plugins = effective_root / "plugins"
        if proj_plugins.is_dir():
            for s_dir, s_label, p_id, p_type in _discover_plugin_skills(proj_plugins):
                # Exclude agent plugins from root skills
                agent_md = s_dir.parent / "agents"
                if not agent_md.is_dir():
                    sources.append((s_dir, "plugin", p_id, p_type, "PLUGIN"))

        # Installed marketplace plugins from store / cache
        try:
            from k8s_autopilot.plugins.discovery import discover_plugins

            plugin_result = discover_plugins(project_root=effective_root, store=store)
            for plugin in plugin_result.plugins:
                inv = getattr(plugin, "inventory", None)
                if inv and getattr(inv, "agents", None):
                    continue  # Agent plugin skills belong exclusively to their subagent

                root = getattr(plugin, "root", None)
                if root and isinstance(root, Path):
                    skills_dir = root / "skills"
                    if skills_dir.is_dir():
                        p_id = getattr(plugin, "plugin_id", getattr(plugin, "name", "plugin"))
                        sources.append((skills_dir, "plugin", p_id, "marketplace", "PLUGIN"))
        except Exception as exc:
            logger.debug("Could not load marketplace plugin skills in list_skills: %s", exc)

    # 5. Optional Subagent skills (when explicitly requested)
    if include_subagents:
        subagents_dir = Path(__file__).parent.parent / "built_in_subagents"
        if subagents_dir.is_dir():
            for sub_dir in sorted(subagents_dir.iterdir()):
                if sub_dir.is_dir():
                    sub_skills = sub_dir / "skills"
                    if sub_skills.is_dir():
                        sources.append((sub_skills, "subagent", sub_dir.name, "subagent", "SUBAGENT"))

    # Scan and parse each source
    for skill_dir, source_label, prefix, p_type, scope_label in sources:
        if not skill_dir or not skill_dir.exists():
            continue
        try:
            backend = FilesystemBackend(root_dir=str(skill_dir), virtual_mode=False)
            skills = list_skills_from_backend(backend=backend, source_path=".")
            for skill in skills:
                name = f"{prefix}:{skill['name']}" if prefix else skill["name"]
                skill_path = Path(skill_dir) / skill["name"]
                skill_md_path = skill_path / "SKILL.md" if skill_path.is_dir() else skill_path

                parsed_meta = _parse_skill_file(skill_md_path)
                desc = parsed_meta["description"] or skill.get("description", "")

                extended: ExtendedSkillMetadata = {
                    "name": name,
                    "description": desc,
                    "path": str(skill_md_path if skill_md_path.is_file() else skill_path),
                    "location": str(skill_path.resolve() if skill_path.is_dir() else skill_path.parent.resolve()),
                    "virtual_path": f"/skills/{name}/SKILL.md",
                    "source": source_label,
                    "scope": scope_label,
                    "plugin_id": prefix or None,
                    "plugin_type": p_type,
                    "license": parsed_meta.get("license"),
                    "compatibility": parsed_meta.get("compatibility"),
                    "tags": parsed_meta.get("tags") or [],
                    "domain": parsed_meta.get("domain") or "",
                    "preview": parsed_meta.get("preview") or desc[:150],
                    "enabled": True,
                    "frontmatter": parsed_meta.get("frontmatter") or {},
                }
                all_skills[name] = extended
        except Exception:
            logger.warning("Could not load skills from %s", skill_dir, exc_info=True)

    return list(all_skills.values())


def load_skill_content(
    skill_path: str,
    *,
    allowed_roots: Sequence[Path] = (),
) -> str | None:
    """Read full raw SKILL.md content for a skill, verifying containment safety (SSRF prevention)."""
    path = Path(skill_path).resolve()
    if path.is_dir():
        path = path / "SKILL.md"

    if allowed_roots:
        resolved_roots = [r.resolve() for r in allowed_roots]
        if not any(path.is_relative_to(root) for root in resolved_roots):
            logger.warning("Skill path %s is outside all allowed roots", skill_path)
            raise PermissionError(
                f"Skill path {skill_path} resolves outside allowed skill roots (SSRF prevention)."
            )

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Could not read skill content from %s: %s", skill_path, e)
        return None


def get_skill_by_name(
    name: str,
    *,
    project_root: Path | None = None,
    store: Any = None,
) -> ExtendedSkillMetadata | None:
    """Get a single skill's metadata by name."""
    skills = list_skills(project_root=project_root, store=store, include_subagents=True)
    for s in skills:
        if s.get("name") == name:
            return s
    return None


def get_skill_content_by_name(
    name: str,
    *,
    project_root: Path | None = None,
    store: Any = None,
) -> tuple[ExtendedSkillMetadata | None, str | None]:
    """Get skill metadata and its raw markdown content by name."""
    skill = get_skill_by_name(name, project_root=project_root, store=store)
    if not skill:
        return None, None
    skill_path = skill.get("path")
    if not skill_path:
        return skill, None
    content = load_skill_content(skill_path)
    return skill, content
