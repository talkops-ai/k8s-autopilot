"""Loader for subagent definitions with Database as Source of Truth and Auto-Rehydration.

Features:
- Discovers subagents from ``built_in_subagents/``, plugins, user, and project directories.
- Synchronizes subagent definitions into the database ``subagents`` table.
- **Redeployment Auto-Rehydration**: If a custom subagent exists in the DB but
  its file is missing on disk after container redeployment, the ``AGENTS.md``
  file is automatically re-created from stored ``system_prompt``.
- Full parity with OpsCode SubagentMetadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from deepagents.middleware.async_subagents import AsyncSubAgent
from k8s_autopilot.config import paths
from k8s_autopilot.subagents.subagents_parser import parse_built_in_subagents
from k8s_autopilot.subagents.types import SubagentMetadata

get_built_in_subagents = parse_built_in_subagents

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_subagent_file(
    file_path: Path, *, fallback_name: str | None = None
) -> SubagentMetadata | None:
    """Parse a subagent markdown file with YAML frontmatter."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Skipping subagent %s: could not read file (%s)", file_path, exc)
        return None

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("Skipping subagent %s: invalid YAML frontmatter (%s)", file_path, exc)
        return None

    if not isinstance(frontmatter, dict):
        return None

    name_value = frontmatter.get("name", fallback_name)
    description_value = frontmatter.get("description")
    model = frontmatter.get("model")
    raw_skills = frontmatter.get("skills")
    raw_tools = frontmatter.get("tools")
    raw_permission_tier = frontmatter.get("permission_tier")

    name = name_value.strip() if isinstance(name_value, str) and name_value.strip() else None
    description = description_value.strip() if isinstance(description_value, str) and description_value.strip() else None

    if name is None or description is None:
        return None

    if isinstance(raw_skills, str):
        skills: list[str] | None = [s.strip() for s in raw_skills.split(",") if s.strip()]
    elif isinstance(raw_skills, list):
        skills = [str(s) for s in raw_skills]
    else:
        skills = None

    if isinstance(raw_tools, str):
        tools: list[str] | None = [t.strip() for t in raw_tools.split(",") if t.strip()]
    elif isinstance(raw_tools, list):
        tools = [str(t) for t in raw_tools]
    else:
        tools = None

    raw_mcp_config = frontmatter.get("mcp_config")
    raw_mcp_files = frontmatter.get("mcp_files")
    raw_capabilities = frontmatter.get("capabilities")

    mcp_config = dict(raw_mcp_config) if isinstance(raw_mcp_config, dict) else None
    mcp_files = [str(f) for f in raw_mcp_files] if isinstance(raw_mcp_files, list) else None
    capabilities = list(raw_capabilities) if isinstance(raw_capabilities, list) else None

    meta: SubagentMetadata = {
        "name": name,
        "description": description,
        "system_prompt": match.group(2).strip(),
        "model": model,
        "skills": skills,
        "tools": tools,
        "permission_tier": raw_permission_tier if isinstance(raw_permission_tier, str) else None,
        "source": "built-in",
        "path": str(file_path),
    }
    if capabilities is not None:
        meta["capabilities"] = capabilities
    if mcp_config is not None:
        meta["mcp_config"] = mcp_config
    if mcp_files is not None:
        meta["mcp_files"] = mcp_files

    return meta


parse_subagent_file = _parse_subagent_file


def _scan_subagents_dir(agents_dir: Path, source: str) -> dict[str, SubagentMetadata]:
    """Scan a directory for subagent bundles with AGENTS.md."""
    subagents: dict[str, SubagentMetadata] = {}
    if not agents_dir.exists() or not agents_dir.is_dir():
        return subagents

    for entry in agents_dir.iterdir():
        if entry.is_dir():
            md_file = entry / "AGENTS.md"
            if not md_file.exists():
                direct_md = entry / f"{entry.name}.md"
                if direct_md.exists():
                    md_file = direct_md
                else:
                    candidates = list(entry.glob("*.md"))
                    if candidates:
                        md_file = candidates[0]

            if md_file.exists():
                parsed = _parse_subagent_file(md_file, fallback_name=entry.name)
                if parsed:
                    parsed["source"] = source
                    bundle_mcp = entry / ".mcp.json"
                    if bundle_mcp.exists() and "mcp_files" not in parsed:
                        parsed["mcp_files"] = [str(bundle_mcp)]
                    subagents[parsed["name"]] = parsed

    return subagents


_load_subagents_from_dir = _scan_subagents_dir


async def list_subagents_async(
    *,
    user_agents_dir: Path | None = None,
    project_agents_dir: Path | None = None,
    include_builtin: bool = True,
    store: Any = None,
) -> list[SubagentMetadata]:
    """List subagents from DB, auto-seeding local definitions and rehydrating missing files."""
    active_store = store
    if active_store is None:
        try:
            from k8s_autopilot.api.settings_routes import _config_store
            active_store = _config_store
        except Exception:
            active_store = None

    discovered_local: dict[str, SubagentMetadata] = {}

    # 1. Built-in subagents
    if include_builtin:
        builtin_dir = Path(__file__).parent.parent / "built_in_subagents"
        discovered_local.update(_scan_subagents_dir(builtin_dir, source="built-in"))

    # 2. User subagents
    user_dir = user_agents_dir or paths.user_agents_dir("k8s-autopilot")
    if user_dir.is_dir():
        discovered_local.update(_scan_subagents_dir(user_dir, source="user"))

    # 3. Project subagents (takes priority over user)
    if project_agents_dir and project_agents_dir.is_dir():
        discovered_local.update(_scan_subagents_dir(project_agents_dir, source="project"))

    # 4. Sync with DB and Rehydrate
    if active_store is not None:
        db_subagents = {a["name"]: a for a in await active_store.list_subagents()}

        # Seed local definitions into DB
        for name, meta in discovered_local.items():
            if name not in db_subagents or meta.get("source") == "project":
                await active_store.upsert_subagent({
                    "name": name,
                    "description": meta.get("description", ""),
                    "model": meta.get("model"),
                    "instructions_path": meta.get("path"),
                    "system_prompt": meta.get("system_prompt", ""),
                    "tools": meta.get("tools") or [],
                    "source": meta.get("source", "built-in"),
                    "enabled": True,
                })

        # Rehydrate missing files from DB (e.g. after container redeployment)
        all_db_subagents = await active_store.list_subagents()
        result: list[SubagentMetadata] = []

        for record in all_db_subagents:
            if not record.get("enabled", True):
                continue

            name = record["name"]
            inst_path = Path(record.get("instructions_path") or (user_dir / name / "AGENTS.md"))

            # If file missing but prompt exists in DB, restore it!
            if not inst_path.exists() and record.get("system_prompt"):
                try:
                    inst_path.parent.mkdir(parents=True, exist_ok=True)
                    frontmatter = {
                        "name": name,
                        "description": record.get("description", ""),
                    }
                    if record.get("model"):
                        frontmatter["model"] = record["model"]
                    if record.get("tools"):
                        frontmatter["tools"] = record["tools"]

                    fm_text = yaml.safe_dump(frontmatter, sort_keys=False)
                    full_text = f"---\n{fm_text}---\n\n{record['system_prompt']}\n"
                    inst_path.write_text(full_text, encoding="utf-8")
                    logger.info("Auto-rehydrated subagent '%s' at %s from DB", name, inst_path)
                except Exception as e:
                    logger.warning("Failed to rehydrate subagent '%s': %s", name, e)

            # Build metadata dict
            subagent_meta: SubagentMetadata = {
                "name": name,
                "description": record.get("description", ""),
                "system_prompt": record.get("system_prompt", ""),
                "model": record.get("model"),
                "tools": record.get("tools"),
                "source": record.get("source", "built-in"),
                "path": str(inst_path),
            }
            result.append(subagent_meta)

        return result

    # Fallback to local discovered subagents
    return list(discovered_local.values())


def list_subagents(
    *,
    user_agents_dir: Path | None = None,
    project_agents_dir: Path | None = None,
    include_builtin: bool = True,
    store: Any = None,
) -> list[SubagentMetadata]:
    """Synchronous list_subagents entrypoint."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    list_subagents_async(
                        user_agents_dir=user_agents_dir,
                        project_agents_dir=project_agents_dir,
                        include_builtin=include_builtin,
                        store=store,
                    ),
                ).result()
        return asyncio.run(
            list_subagents_async(
                user_agents_dir=user_agents_dir,
                project_agents_dir=project_agents_dir,
                include_builtin=include_builtin,
                store=store,
            )
        )
    except Exception:
        discovered: dict[str, SubagentMetadata] = {}
        if include_builtin:
            builtin_dir = Path(__file__).parent.parent / "built_in_subagents"
            discovered.update(_scan_subagents_dir(builtin_dir, source="built-in"))
        if user_agents_dir and user_agents_dir.is_dir():
            discovered.update(_scan_subagents_dir(user_agents_dir, source="user"))
        if project_agents_dir and project_agents_dir.is_dir():
            discovered.update(_scan_subagents_dir(project_agents_dir, source="project"))
        return list(discovered.values())



async def load_async_subagents_async(
    config_path: Path | None = None,
    *,
    store: Any = None,
) -> list[AsyncSubAgent]:
    """Load async subagent definitions asynchronously from database store or config.

    In k8s-autopilot, the database is the primary source of truth for centralized deployment.
    Definitions are discovered from:
    1. The database ConfigStore ('async_subagents' / 'ASYNC_SUBAGENTS' config entry,
       or subagents table records with is_async=True / graph_id).
    2. Optional fallback to config.toml if provided or found on disk.
    """
    import inspect
    import os

    active_store = store
    if active_store is None:
        try:
            from k8s_autopilot.api.settings_routes import _config_store
            active_store = _config_store
        except Exception:
            active_store = None

    agents: list[AsyncSubAgent] = []
    seen_names: set[str] = set()

    # 1. Database as Source of Truth
    if active_store is not None:
        try:
            raw_async_cfg = None
            if hasattr(active_store, "get"):
                res = active_store.get("async_subagents")
                if inspect.isawaitable(res):
                    res = await res
                if not res:
                    res = active_store.get("ASYNC_SUBAGENTS")
                    if inspect.isawaitable(res):
                        res = await res
                raw_async_cfg = res

            if raw_async_cfg:
                parsed = json.loads(raw_async_cfg) if isinstance(raw_async_cfg, str) else raw_async_cfg
                items = parsed if isinstance(parsed, list) else (list(parsed.values()) if isinstance(parsed, dict) else [])
                for spec in items:
                    if isinstance(spec, dict) and "name" in spec and "description" in spec and "graph_id" in spec:
                        agent: AsyncSubAgent = {
                            "name": spec["name"],
                            "description": spec["description"],
                            "graph_id": spec["graph_id"],
                        }
                        if "url" in spec and isinstance(spec["url"], str):
                            agent["url"] = os.path.expandvars(spec["url"])
                        if "headers" in spec and isinstance(spec["headers"], dict):
                            agent["headers"] = {
                                k: os.path.expandvars(str(v)) for k, v in spec["headers"].items()
                            }
                        agents.append(agent)
                        seen_names.add(agent["name"])

            if hasattr(active_store, "list_subagents"):
                res = active_store.list_subagents()
                if inspect.isawaitable(res):
                    res = await res
                db_records = res or []
                for rec in db_records:
                    rec_name = rec.get("name")
                    if not rec_name or rec_name in seen_names:
                        continue
                    if rec.get("is_async") or rec.get("type") == "async" or rec.get("graph_id"):
                        graph_id = rec.get("graph_id") or rec.get("model") or "agent"
                        agent_spec: AsyncSubAgent = {
                            "name": rec_name,
                            "description": rec.get("description", ""),
                            "graph_id": str(graph_id),
                        }
                        if rec.get("url"):
                            agent_spec["url"] = os.path.expandvars(str(rec["url"]))
                        if isinstance(rec.get("headers"), dict):
                            agent_spec["headers"] = {
                                k: os.path.expandvars(str(v)) for k, v in rec["headers"].items()
                            }
                        agents.append(agent_spec)
                        seen_names.add(rec_name)
        except Exception as exc:
            logger.debug("Could not load async subagents from database: %s", exc)

    # 2. Config file fallback (if provided or present on disk)
    resolved_config_path = config_path
    if resolved_config_path is None:
        try:
            from k8s_autopilot.config.paths import CONFIG_PATH
            if CONFIG_PATH.exists():
                resolved_config_path = CONFIG_PATH
        except Exception:
            pass

    if resolved_config_path is not None and resolved_config_path.exists():
        try:
            import tomllib
            with resolved_config_path.open("rb") as f:
                data = tomllib.load(f)
            section = data.get("async_subagents")
            if isinstance(section, dict):
                required = {"description", "graph_id"}
                for name, spec in section.items():
                    if not isinstance(spec, dict) or name in seen_names:
                        continue
                    if not required.issubset(spec.keys()):
                        continue
                    agent_entry: AsyncSubAgent = {
                        "name": name,
                        "description": spec["description"],
                        "graph_id": spec["graph_id"],
                    }
                    if "url" in spec and isinstance(spec["url"], str):
                        agent_entry["url"] = os.path.expandvars(spec["url"])
                    if "headers" in spec and isinstance(spec["headers"], dict):
                        agent_entry["headers"] = {
                            k: os.path.expandvars(str(v)) for k, v in spec["headers"].items()
                        }
                    agents.append(agent_entry)
                    seen_names.add(name)
        except Exception as exc:
            logger.debug("Could not read async subagents from %s: %s", resolved_config_path, exc)

    return agents


def load_async_subagents(
    config_path: Path | None = None,
    *,
    store: Any = None,
) -> list[AsyncSubAgent]:
    """Load async subagent definitions (synchronous wrapper around load_async_subagents_async)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                load_async_subagents_async(config_path=config_path, store=store),
            ).result()
    else:
        return asyncio.run(
            load_async_subagents_async(config_path=config_path, store=store)
        )
