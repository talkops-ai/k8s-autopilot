"""Skill registry to discover, validate, and index skills with Database as Source of Truth.

Supports:
- Built-in skills (packaged in ``built_in_skills/``)
- Claude plugin skills & Codex/OpenAI plugin skills
- Subagent bundled skills
- Database-backed persistence and container redeployment auto-rehydration
- Name validation regex and SSRF path containment checks
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml

from k8s_autopilot.config import paths
from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.skills.loader import ExtendedSkillMetadata, list_skills
from k8s_autopilot.subagents.types import SubagentMetadata

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class SkillMetadata(TypedDict):
    name: str
    description: str
    domain: str
    path: str
    virtual_path: str
    frontmatter: dict[str, Any]
    system_prompt: str


@dataclass(frozen=True)
class SkillSource:
    """A discovered skill."""

    name: str
    """Unique skill name (directory name)."""

    path: Path
    """Path to the skill directory."""

    tier: str = "built-in"
    """Discovery tier: 'built-in', 'user', 'project', 'plugin', 'subagent'."""

    description: str = ""
    """Description from SKILL.md frontmatter."""

    domain: str = ""
    """Domain category."""

    tags: tuple[str, ...] = ()
    """Tags from SKILL.md frontmatter."""

    enabled: bool = True
    """Whether the skill is active."""


def _parse_skill_frontmatter(skill_md: Path) -> tuple[str, tuple[str, ...]]:
    """Extract description and tags from SKILL.md."""
    try:
        content = skill_md.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if match:
            fm = yaml.safe_load(match.group(1))
            if isinstance(fm, dict):
                desc = fm.get("description", "")
                tags = tuple(fm.get("tags", []))
                return str(desc), tags
    except Exception as exc:
        logger.debug("Failed parsing frontmatter from %s: %s", skill_md, exc)
    return "", ()


class SkillRegistry:
    """Discovers, validates, and indexes skills with DB synchronization and auto-rehydration."""

    _instance: SkillRegistry | None = None
    _lock = threading.Lock()

    def __init__(self, store: Any = None) -> None:
        self._skills: dict[str, SkillMetadata] = {}
        self._sources: dict[str, SkillSource] = {}
        self._store = store

    @classmethod
    def get_instance(cls, store: Any = None) -> SkillRegistry:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(store=store)
        elif store is not None:
            cls._instance._store = store
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset in-memory skill caches while preserving store reference if set."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance._skills.clear()
                cls._instance._sources.clear()

    def clear(self) -> None:
        """Clear registered skills in-memory."""
        self._skills.clear()
        self._sources.clear()

    def _get_store(self) -> Any:
        if self._store is None:
            from k8s_autopilot.api.settings_routes import _config_store
            self._store = _config_store
        return self._store

    def register(self, skill: SkillSource) -> None:
        self._sources[skill.name] = skill

    def get(self, name: str) -> SkillSource | None:
        return self._sources.get(name)

    def get_skill(self, name: str) -> SkillMetadata | None:
        self.discover_skills()
        return self._skills.get(name)

    def list_skills(self, tier: str | None = None) -> list[SkillSource]:
        skills = list(self._sources.values())
        if tier:
            skills = [s for s in skills if s.tier == tier or (tier == "builtin" and s.tier == "built-in")]
        return sorted([s for s in skills if s.enabled], key=lambda s: s.name)

    def discover(
        self,
        *,
        builtin_dir: Path | None = None,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> int:
        """Discover skills directly from filesystem directory tiers."""
        count = 0
        tiers = [
            ("builtin", builtin_dir),
            ("user", user_dir),
            ("project", project_dir),
        ]
        for tier_name, tier_dir in tiers:
            if tier_dir is None or not tier_dir.is_dir():
                continue
            for skill_dir in sorted(tier_dir.iterdir()):
                skill_md = skill_dir / "SKILL.md"
                if skill_dir.is_dir() and skill_md.exists():
                    desc, tags = _parse_skill_frontmatter(skill_md)
                    self.register(
                        SkillSource(
                            name=skill_dir.name,
                            path=skill_dir,
                            tier=tier_name,
                            description=desc,
                            domain="",
                            tags=tags,
                            enabled=True,
                        )
                    )
                    count += 1
        return count

    def get_sources_for_middleware(
        self,
        project_root: Path | None = None,
        include_subagent_skills: bool = False,
        subagents: Sequence[SubagentMetadata] | None = None,
    ) -> list[tuple[str, ...]]:
        """Return sources ordered by precedence for SkillsMiddleware.

        When ``include_subagent_skills`` is False (default), agent-bound plugin skills
        are excluded so that the main agent maintains context isolation. When True,
        all skills across agents, agent plugins, and subagents are returned.
        """
        settings = get_settings()
        effective_project_root = project_root or getattr(settings, "project_root", None) or Path.cwd()
        sources: list[tuple[str, ...]] = []

        # 1. Built-in skills
        built_in_dir = Path(__file__).parent.parent / "built_in_skills"
        if built_in_dir.is_dir():
            sources.append((str(built_in_dir), "Built-in"))

        # 2. Plugin skills
        try:
            from k8s_autopilot.plugins.adapters.skills import plugin_skill_sources
            from k8s_autopilot.plugins.discovery import discover_plugins
            active_store = self._store or self._get_store()
            plugin_result = discover_plugins(project_root=effective_project_root, store=active_store)
            for plugin in plugin_result.plugins:
                if not include_subagent_skills:
                    inv = getattr(plugin, "inventory", None)
                    if inv and getattr(inv, "agents", None):
                        has_agents = any(
                            (p.is_dir() and any(p.iterdir())) or (p.is_file() and p.suffix == ".md")
                            for p in inv.agents
                            if p.exists()
                        )
                        if has_agents:
                            continue  # Agent plugin skills are bound exclusively to their subagent
                for skill_src, label, p_id in plugin_skill_sources([plugin]):
                    if Path(skill_src).is_dir():
                        sources.append((skill_src, label, p_id))
        except Exception as exc:
            logger.debug("Could not discover marketplace plugin skills for middleware: %s", exc)

        # 3. Local project plugins (project_root/plugins)
        proj_plugins_dir = effective_project_root / "plugins"
        if proj_plugins_dir.is_dir():
            for p in sorted(proj_plugins_dir.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    if not include_subagent_skills:
                        agents_dir = p / "agents"
                        if agents_dir.is_dir() and any(agents_dir.iterdir()):
                            continue  # Agent plugin skills are bound exclusively to their subagent
                    skills_dir = p / "skills"
                    if skills_dir.is_dir():
                        p_source = (str(skills_dir), f"Plugin: {p.name}", p.name)
                        if not any(s[0] == p_source[0] for s in sources):
                            sources.append(p_source)

        # 4. User skills
        user_skills_dir = paths.user_skills_dir(settings.assistant_id)
        if user_skills_dir.is_dir():
            sources.append((str(user_skills_dir), "User"))
        dot_agents_user_skills = Path.home() / ".agents" / "skills"
        if dot_agents_user_skills.is_dir() and not any(s[0] == str(dot_agents_user_skills) for s in sources):
            sources.append((str(dot_agents_user_skills), "User"))

        # 5. Project skills
        proj_skills_candidates = [
            paths.project_skills_dir(effective_project_root),
            effective_project_root / "skills",
            effective_project_root / ".agents" / "skills",
        ]
        for p_dir in proj_skills_candidates:
            if p_dir.is_dir() and not any(s[0] == str(p_dir) for s in sources):
                sources.append((str(p_dir), "Project"))

        # 6. Bundled subagent skills (when requested)
        if include_subagent_skills:
            target_subagents: list[SubagentMetadata] = list(subagents) if subagents is not None else []
            if not target_subagents:
                try:
                    from k8s_autopilot.subagents.loader import get_built_in_subagents, list_subagents
                    target_subagents.extend(get_built_in_subagents())
                    target_subagents.extend(list_subagents(store=self._store or self._get_store()))
                except Exception as exc:
                    logger.debug("Could not discover subagents for bundled skills: %s", exc)

            for sub_meta in target_subagents:
                sub_name = sub_meta.get("name", "subagent")
                sub_path = sub_meta.get("path")
                if sub_path:
                    p = Path(sub_path)
                    bundle_dir = p.parent.parent if p.parent.name == "agents" else p.parent
                    sub_skills_dir = bundle_dir / "skills"
                    if sub_skills_dir.is_dir():
                        sub_source = (str(sub_skills_dir), f"Subagent ({sub_name})")
                        if not any(s[0] == sub_source[0] for s in sources):
                            sources.append(sub_source)

        return [source for source in sources if Path(source[0]).exists()]

    async def sync_with_db_async(
        self,
        project_root: Path | None = None,
        store: Any = None,
    ) -> list[SkillSource]:
        """Scan local filesystem tiers, sync/rehydrate with DB, and return active skills."""
        active_store = store or self._get_store()
        root = project_root or Path.cwd()

        builtin_dir = Path(__file__).parent.parent / "built_in_skills"
        user_dir = paths.user_skills_dir("k8s-autopilot")
        project_dir = root / ".k8s_autopilot" / "skills"

        self._skills.clear()
        self._sources.clear()

        # List all skills using multi-tier loader (includes plugins & subagents)
        discovered = list_skills(
            built_in_skills_dir=builtin_dir,
            user_skills_dir=user_dir,
            project_skills_dir=project_dir,
            project_root=root,
            include_plugins=True,
        )

        name_pattern = re.compile(r"^[a-zA-Z0-9_:-]+$")

        for skill in discovered:
            name = skill.get("name", "")
            if not name or not name_pattern.match(name):
                continue

            path = Path(skill.get("path", ""))
            skill_md = path if path.is_file() else (path / "SKILL.md")
            content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
            desc, tags = _parse_skill_frontmatter(skill_md) if skill_md.exists() else (skill.get("description", ""), ())

            tier = skill.get("source", "built-in")
            self._skills[name] = SkillMetadata(
                name=name,
                description=desc or skill.get("description", ""),
                domain="k8s",
                path=str(path),
                virtual_path=f"/skills/{name}/SKILL.md",
                frontmatter={},
                system_prompt=content,
            )

            self.register(
                SkillSource(
                    name=name,
                    path=path,
                    tier=tier,
                    description=desc or skill.get("description", ""),
                    domain="k8s",
                    tags=tags,
                    enabled=True,
                )
            )

        # Sync with DB & Rehydrate if missing on disk
        # Strategy: built-in and subagent skills are always part of the codebase,
        # so they do NOT need DB persistence. Only plugin/user/project skills are
        # persisted because they may be installed at runtime and need to survive
        # container redeployments.
        _SKIP_DB_TIERS = frozenset(("built-in", "subagent"))

        if active_store is not None:
            db_skills = {s["name"]: s for s in await active_store.list_skills()}
            for name, meta in self._skills.items():
                source_tier = self._sources[name].tier if name in self._sources else "built-in"
                # Only persist plugin, user, or project skills into DB
                if source_tier not in _SKIP_DB_TIERS and name not in db_skills:
                    await active_store.upsert_skill({
                        "name": name,
                        "description": meta["description"],
                        "domain": meta["domain"],
                        "path": meta["path"],
                        "virtual_path": meta["virtual_path"],
                        "source": source_tier,
                        "content": meta["system_prompt"],
                        "enabled": True,
                    })

            # Rehydrate missing files from DB records and prune stale records
            all_db_skills = await active_store.list_skills()
            for s in all_db_skills:
                s_name = s.get("name", "")
                s_path_str = s.get("path", "")
                s_source = s.get("source", "")

                # Purge stale built-in/subagent DB entries (they should never be in DB)
                if s_source in _SKIP_DB_TIERS:
                    await active_store.delete_skill(s_name)
                    logger.debug("Removed stale %s skill '%s' from DB (always in codebase)", s_source, s_name)
                    continue

                # Clean up stale DB skill entries that pointed to caches, marketplaces, or uninstalled plugins
                if (
                    "cache" in s_path_str
                    or "marketplaces" in s_path_str
                    or (s_source == "plugin" and s_name not in self._skills)
                ):
                    if not Path(s_path_str).exists() or s_name not in self._skills:
                        await active_store.delete_skill(s_name)
                        continue

                skill_path = Path(s.get("path") or (user_dir / s["name"]))
                skill_md = skill_path / "SKILL.md"

                # Rehydrate only plugin/user/project skills missing from disk
                if not skill_md.exists() and s.get("content") and s_source in ("plugin", "user", "project"):
                    try:
                        skill_path.mkdir(parents=True, exist_ok=True)
                        skill_md.write_text(s["content"], encoding="utf-8")
                        logger.info("Auto-rehydrated skill '%s' at %s from DB", s["name"], skill_md)
                    except Exception as e:
                        logger.warning("Failed to rehydrate skill '%s': %s", s["name"], e)

                if s_name not in self._sources:
                    source_obj = SkillSource(
                        name=s["name"],
                        path=skill_path,
                        tier=s.get("source", "built-in"),
                        description=s.get("description", ""),
                        domain=s.get("domain", ""),
                        enabled=s.get("enabled", True),
                    )
                    self.register(source_obj)

            return self.list_skills()

        return self.list_skills()

    def discover_skills(self, project_root: Path | None = None, force: bool = False) -> list[SkillSource]:
        """Synchronous discovery entrypoint."""
        if self._sources and not force:
            return list(self._sources.values())
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        asyncio.run, self.sync_with_db_async(project_root)
                    ).result()
            return loop.run_until_complete(self.sync_with_db_async(project_root))
        except Exception:
            return list(self._sources.values())


def get_skill_registry(store: Any = None) -> SkillRegistry:
    """Return the singleton SkillRegistry instance."""
    return SkillRegistry.get_instance(store=store)
