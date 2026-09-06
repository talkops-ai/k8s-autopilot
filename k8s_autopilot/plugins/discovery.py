"""Plugin discovery, install, marketplace catalog, and enablement for K8s Autopilot."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k8s_autopilot.plugins.manifest import (
    PluginManifestError,
    build_inventory,
    inspect_plugin_components,
    load_manifest,
)
from k8s_autopilot.plugins.marketplace import (
    MarketplaceError,
    load_marketplace,
    load_marketplace_location,
    materialize_marketplace_source,
    materialize_plugin_source,
    parse_marketplace_source,
    redact_urls_in_text,
)
from k8s_autopilot.plugins.models import (
    ComponentInventory,
    InstallScope,
    MarketplacePluginEntry,
    MarketplaceRecord,
    PluginDiscoveryResult,
    PluginInstance,
    PluginMarketplace,
    RepositoryMarketplaceSource,
    split_plugin_id,
)
from k8s_autopilot.plugins.store import (
    add_installed_plugin_entry_async,
    cache_and_register_plugin,
    delete_marketplace_record_async,
    ensure_marketplace_cache_dir,
    ensure_plugin_data_dir,
    load_all_enabled_plugin_ids_async,
    load_installed_plugins_async,
    load_marketplace_records_async,
    plugin_data_dir,
    remove_installed_plugin_entry_async,
    save_marketplace_record_async,
    set_plugin_enabled_async as set_db_plugin_enabled_async,
)

if TYPE_CHECKING:
    from k8s_autopilot.config.store import ConfigStore

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


def _get_active_store(store: ConfigStore | None = None) -> ConfigStore | None:
    if store is not None:
        return store
    try:
        from k8s_autopilot.api.settings_routes import _config_store
        if _config_store is not None:
            return _config_store
    except Exception:
        pass
    try:
        from k8s_autopilot.config.store_factory import create_config_store_sync

        return create_config_store_sync()
    except Exception as exc:
        logger.debug("Could not auto-create config store from store_factory: %s", exc)
    return None


async def add_marketplace_source_async(
    raw: str, store: ConfigStore | None = None
) -> PluginMarketplace:
    """Add a marketplace from a URL, git repo (owner/repo), or local path and save to DB."""
    active_store = _get_active_store(store)
    source = parse_marketplace_source(raw)
    marketplace, location = materialize_marketplace_source(source)
    if active_store is not None:
        await save_marketplace_record_async(
            MarketplaceRecord(
                name=marketplace.name,
                source_type=source.source_type,
                source=source.value,
                install_location=str(location),
                ref=source.ref if isinstance(source, RepositoryMarketplaceSource) else None,
                plugin_count=len(marketplace.plugins),
            ),
            active_store,
        )
    return marketplace


async def remove_marketplace_async(name: str, store: ConfigStore | None = None) -> bool:
    """Remove a marketplace and uninstall every plugin associated with it."""
    active_store = _get_active_store(store)
    if active_store is None:
        return False

    records = await load_marketplace_records_async(active_store)
    record = records.get(name)
    if record is None:
        return False

    # Uninstall all plugins belonging to this marketplace
    installed_plugins = await load_installed_plugins_async(active_store)
    for p_id in list(installed_plugins.keys()):
        _, m_name = split_plugin_id(p_id)
        if m_name == name:
            await uninstall_plugin_async(p_id, store=active_store)

    removed = await delete_marketplace_record_async(name, active_store)

    # Clean up cached repository files if present
    location = Path(record.install_location)
    try:
        resolved = location.resolve()
        cache_root = ensure_marketplace_cache_dir().resolve()
        if record.source_type in {"github", "git", "url"} and resolved.is_relative_to(cache_root):
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=True)
            elif resolved.is_file():
                resolved.unlink(missing_ok=True)
    except OSError:
        pass

    return removed


async def _ensure_marketplace_materialized_async(
    record: MarketplaceRecord, store: ConfigStore | None = None
) -> PluginMarketplace:
    """Ensure marketplace files exist on disk, re-materializing from source if missing."""
    location = Path(record.install_location)
    if location.exists():
        try:
            return load_marketplace_location(location)
        except Exception:
            pass

    # Location missing on disk (e.g. fresh container startup with persistent DB)
    logger.info(
        "Re-materializing marketplace %s from %s (%s)",
        record.name,
        record.source,
        record.source_type,
    )
    source = parse_marketplace_source(record.source)
    marketplace, new_location = materialize_marketplace_source(source)
    if store is not None and str(new_location) != record.install_location:
        updated_record = dataclasses.replace(
            record,
            install_location=str(new_location),
            plugin_count=len(marketplace.plugins),
        )
        await save_marketplace_record_async(updated_record, store)
    return marketplace


async def _resolve_marketplace_and_entry_async(
    plugin_id: str, store: ConfigStore
) -> tuple[PluginMarketplace, MarketplacePluginEntry]:
    try:
        plugin_name, marketplace_name = split_plugin_id(plugin_id)
    except ValueError as exc:
        raise MarketplaceError(str(exc)) from exc

    records = await load_marketplace_records_async(store)
    record = records.get(marketplace_name)
    if record is None:
        msg = f"Marketplace {marketplace_name!r} is not configured"
        raise MarketplaceError(msg)

    marketplace = await _ensure_marketplace_materialized_async(record, store)
    entry = next(
        (plugin for plugin in marketplace.plugins if plugin.name == plugin_name),
        None,
    )
    if entry is None:
        msg = f"Plugin {plugin_id!r} not found in marketplace {marketplace_name}"
        raise MarketplaceError(msg)
    return marketplace, entry


def _plugin_from_install_path(
    *,
    plugin_id: str,
    root: Path,
    marketplace_name: str,
    fallback_name: str,
) -> tuple[PluginInstance | None, tuple[str, ...]]:
    warnings: list[str] = []
    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            root, fallback_name=fallback_name
        )
    except PluginManifestError as exc:
        return None, (f"Skipping plugin {plugin_id}: {exc}",)
    warnings.extend(manifest_warnings)
    name = manifest.name if manifest and manifest.name else fallback_name
    inventory = build_inventory(root, manifest, tuple(warnings))
    try:
        instance = PluginInstance(
            plugin_id=plugin_id,
            name=name,
            marketplace=marketplace_name,
            version=manifest.version if manifest is not None else "1.0.0",
            root=root,
            data_dir=plugin_data_dir(plugin_id),
            manifest=manifest,
            inventory=inventory,
        )
    except ValueError as exc:
        return None, (f"Skipping plugin {plugin_id}: {exc}",)
    return instance, inventory.warnings


async def install_plugin_async(
    plugin_id: str,
    *,
    scope: InstallScope = "global",
    store: ConfigStore | None = None,
) -> PluginInstance:
    """Install a marketplace plugin into the versioned cache and record in DB."""
    active_store = _get_active_store(store)
    if active_store is None:
        msg = "No ConfigStore available for plugin installation"
        raise RuntimeError(msg)

    marketplace, entry = await _resolve_marketplace_and_entry_async(plugin_id, active_store)
    source_root = materialize_plugin_source(marketplace, entry)
    if source_root is None:
        msg = (
            f"Plugin {plugin_id} has unsupported source "
            f"{redact_urls_in_text(repr(entry.source))}; "
            "use a local path, GitHub repository, or Git repository source"
        )
        raise MarketplaceError(msg)

    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            source_root, fallback_name=entry.name
        )
    except PluginManifestError as exc:
        msg = f"Cannot install {plugin_id}: {exc}"
        raise MarketplaceError(msg) from exc

    version = manifest.version if manifest is not None else "1.0.0"
    cache_path = cache_and_register_plugin(
        plugin_id,
        source_root,
        version=version,
    )

    ensure_plugin_data_dir(plugin_id)

    instance, warnings = _plugin_from_install_path(
        plugin_id=plugin_id,
        root=cache_path,
        marketplace_name=marketplace.name,
        fallback_name=entry.name,
    )
    if instance is None:
        msg = f"Installed {plugin_id} but failed to load from cache: {'; '.join(warnings)}"
        raise MarketplaceError(msg)

    # Inspect components accurately from cached filesystem
    comp = inspect_plugin_components(cache_path, instance.manifest)
    display_name = comp["display_name"] or entry.display_name or entry.name
    description = comp["description"] or entry.description or ""
    author = comp["author"] or (entry.author if isinstance(entry.author, str) else (entry.author.get("name") if isinstance(entry.author, dict) else None))

    await add_installed_plugin_entry_async(
        plugin_id,
        install_path=cache_path,
        version=comp["version"] or version,
        scope=scope,
        display_name=display_name,
        description=description,
        author=author,
        skill_count=comp["skill_count"],
        skill_names=comp["skill_names"],
        mcp_server_names=comp["mcp_server_names"],
        source_type=getattr(entry.source, "source_type", "local"),
        source_value=getattr(entry.source, "path", None) or getattr(entry.source, "url", None) or getattr(entry.source, "repo", None),
        store=active_store,
    )

    # Auto-seed discovered plugin MCP server configs into DB
    mcp_file = cache_path / ".mcp.json"
    if mcp_file.is_file():
        try:
            with open(mcp_file, "r", encoding="utf-8") as f:
                mcp_data = json.load(f)
            servers = mcp_data.get("mcpServers") if isinstance(mcp_data, dict) else None
            if isinstance(servers, dict):
                for s_name, s_conf in servers.items():
                    if isinstance(s_conf, dict):
                        url = s_conf.get("url") or s_conf.get("serverUrl")
                        command = s_conf.get("command")
                        transport = s_conf.get("transport") or s_conf.get("type")
                        if not transport:
                            if url:
                                transport = "sse" if "sse" in str(url).lower() else "http"
                            else:
                                transport = "stdio"
                        await active_store.upsert_mcp_server({
                            "name": s_name,
                            "transport": transport,
                            "command": command,
                            "args": s_conf.get("args", []),
                            "url": url,
                            "env": s_conf.get("env", {}),
                            "headers": s_conf.get("headers", {}),
                            "source": f"plugin:{entry.name}",
                            "disabled_tools": s_conf.get("disabled_tools") or s_conf.get("disabledTools") or [],
                            "allowed_tools": s_conf.get("allowed_tools") or s_conf.get("allowedTools") or [],
                            "enabled": not s_conf.get("disabled", False),
                            "trusted": False,
                        })
        except Exception as exc:
            logger.debug("Failed to auto-seed plugin MCP servers: %s", exc)

    return instance


async def uninstall_plugin_async(
    plugin_id: str,
    store: ConfigStore | None = None,
) -> bool:
    """Uninstall a plugin (disable, remove DB records, clear cache, and purge related skill records)."""
    active_store = _get_active_store(store)
    if active_store is None:
        return False

    plugin_name, _ = split_plugin_id(plugin_id)
    plugin_record = await active_store.get_plugin(plugin_id)
    plugin_skills: set[str] = set()
    install_path_str = ""
    if plugin_record:
        install_path_str = str(plugin_record.get("install_path") or "")
        raw_skills = plugin_record.get("skill_names")
        if isinstance(raw_skills, list):
            plugin_skills.update(raw_skills)
        elif isinstance(raw_skills, str):
            try:
                parsed = json.loads(raw_skills)
                if isinstance(parsed, list):
                    plugin_skills.update(parsed)
            except Exception:
                pass

    removed = await remove_installed_plugin_entry_async(plugin_id, active_store)

    # Clean up all corresponding DB skill records for this plugin
    try:
        db_skills = await active_store.list_skills()
        for s in db_skills:
            s_name = s.get("name", "")
            s_path = s.get("path", "")
            s_source = s.get("source", "")
            if (
                s_name.startswith(f"{plugin_id}:")
                or s_name.startswith(f"{plugin_name}:")
                or s_name in plugin_skills
                or (install_path_str and install_path_str in s_path)
                or plugin_id in s_path
                or (s_source == "plugin" and plugin_name in s_path)
            ):
                await active_store.delete_skill(s_name)
    except Exception as exc:
        logger.debug("Failed cleaning up plugin skills from store: %s", exc)

    # Clean up corresponding MCP servers for this plugin
    try:
        db_mcp_servers = await active_store.list_mcp_servers()
        for srv in db_mcp_servers:
            s_name = srv.get("name", "")
            s_source = srv.get("source", "")
            if s_source in (f"plugin:{plugin_name}", f"plugin:{plugin_id}") or (
                plugin_record and s_name in (plugin_record.get("mcp_server_names") or [])
            ):
                await active_store.delete_mcp_server(s_name)
    except Exception as exc:
        logger.debug("Failed cleaning up plugin MCP servers from store: %s", exc)

    # Invalidate SkillRegistry in-memory caches
    try:
        from k8s_autopilot.skills.registry import SkillRegistry
        SkillRegistry.reset()
    except Exception:
        pass

    return removed


async def set_plugin_enabled_async(
    plugin_id: str,
    enabled: bool,
    store: ConfigStore | None = None,
) -> None:
    """Enable or disable a plugin in the database."""
    active_store = _get_active_store(store)
    if active_store is not None:
        await set_db_plugin_enabled_async(plugin_id, enabled, active_store)
        if enabled:
            ensure_plugin_data_dir(plugin_id)

        # Toggle corresponding MCP servers
        try:
            plugin_record = await active_store.get_plugin(plugin_id)
            plugin_name, _ = split_plugin_id(plugin_id)
            db_mcp_servers = await active_store.list_mcp_servers()
            for srv in db_mcp_servers:
                s_source = srv.get("source", "")
                s_name = srv.get("name", "")
                if s_source in (f"plugin:{plugin_name}", f"plugin:{plugin_id}") or (
                    plugin_record and s_name in (plugin_record.get("mcp_server_names") or [])
                ):
                    srv["enabled"] = enabled
                    await active_store.upsert_mcp_server(srv)
        except Exception as exc:
            logger.debug("Failed toggling plugin MCP servers: %s", exc)

        try:
            from k8s_autopilot.skills.registry import SkillRegistry
            SkillRegistry.reset()
        except Exception:
            pass


async def discover_plugins_async(
    store: ConfigStore | None = None,
    project_root: Path | None = None,
) -> PluginDiscoveryResult:
    """Discover enabled marketplace plugins and local plugins with auto-rehydration."""
    active_store = _get_active_store(store)
    plugins: list[PluginInstance] = []
    warnings: list[str] = []

    # 1. Local filesystem discovery (e.g. project plugins directory)
    if project_root:
        proj_plugins = project_root / "plugins"
        if proj_plugins.is_dir():
            for p in sorted(proj_plugins.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    inst, p_warns = _plugin_from_install_path(
                        plugin_id=f"{p.name}@project",
                        root=p,
                        marketplace_name="project",
                        fallback_name=p.name,
                    )
                    warnings.extend(p_warns)
                    if inst:
                        plugins.append(inst)

    # 2. Database-backed marketplace plugins
    if active_store is not None:
        enabled_ids = await load_all_enabled_plugin_ids_async(active_store)
        installed_map = await load_installed_plugins_async(active_store)

        for plugin_id in sorted(installed_map.keys()):
            plugin_name, marketplace_name = split_plugin_id(plugin_id)
            entry = installed_map[plugin_id]
            is_enabled = plugin_id in enabled_ids
            root = Path(entry.install_path)

            if not root.is_dir():
                try:
                    logger.info("Auto-rehydrating missing plugin cache for %s", plugin_id)
                    instance = await install_plugin_async(plugin_id, store=active_store)
                    if is_enabled and instance is not None and not any(p.plugin_id == instance.plugin_id for p in plugins):
                        plugins.append(instance)
                    continue
                except Exception as exc:
                    warnings.append(
                        f"Plugin {plugin_id} is recorded in DB but auto-rehydration failed: {exc}"
                    )
                    continue

            try:
                plugin, plugin_warnings = _plugin_from_install_path(
                    plugin_id=plugin_id,
                    root=root,
                    marketplace_name=marketplace_name,
                    fallback_name=plugin_name,
                )
            except (OSError, RuntimeError) as exc:
                warnings.append(f"Skipping plugin {plugin_id}: {exc}")
                continue
            warnings.extend(plugin_warnings)
            if is_enabled and plugin is not None and not any(p.plugin_id == plugin.plugin_id for p in plugins):
                plugins.append(plugin)

    return PluginDiscoveryResult(plugins=tuple(plugins), warnings=tuple(warnings))


async def list_available_plugins_async(
    store: ConfigStore | None = None,
    project_root: Path | None = None,
    include_installed: bool = False,
) -> list[dict[str, Any]]:
    """List all available plugins across all registered marketplaces with auto-rehydration."""
    active_store = _get_active_store(store)
    if active_store is None:
        return []

    records = await load_marketplace_records_async(active_store)
    installed = await load_installed_plugins_async(active_store)
    enabled = await load_all_enabled_plugin_ids_async(active_store)

    results: list[dict[str, Any]] = []

    for name, record in sorted(records.items()):
        try:
            marketplace = await _ensure_marketplace_materialized_async(record, active_store)
        except Exception as exc:
            logger.warning("Could not load/materialize marketplace %s: %s", name, exc)
            continue

        for plugin in marketplace.plugins:
            plugin_id = f"{plugin.name}@{marketplace.name}"
            is_installed = plugin_id in installed
            is_enabled = plugin_id in enabled

            if is_installed and not include_installed:
                continue

            source_root = None
            try:
                source_root = materialize_plugin_source(marketplace, plugin)
            except Exception:
                pass

            if source_root and source_root.exists():
                comp = inspect_plugin_components(source_root)
                display_name = comp["display_name"] or plugin.display_name or plugin.name
                description = comp["description"] or plugin.description or ""
                version = comp["version"]
                author = comp["author"] or (plugin.author if isinstance(plugin.author, str) else (plugin.author.get("name") if isinstance(plugin.author, dict) else None))
                skill_names = comp["skill_names"]
                skill_count = comp["skill_count"]
                mcp_server_names = comp["mcp_server_names"]
            else:
                display_name = plugin.display_name or plugin.name
                description = plugin.description or ""
                version = "1.0.0"
                author = plugin.author if isinstance(plugin.author, str) else (plugin.author.get("name") if isinstance(plugin.author, dict) else None)
                skill_names = []
                skill_count = 0
                mcp_server_names = []

            results.append({
                "plugin_id": plugin_id,
                "name": plugin.name,
                "display_name": display_name,
                "description": description,
                "version": version,
                "author": author,
                "marketplace": marketplace.name,
                "installed": is_installed,
                "enabled": is_enabled,
                "skill_count": skill_count,
                "skill_names": skill_names,
                "mcp_server_names": mcp_server_names,
                "source_type": getattr(plugin.source, "source_type", "local"),
            })

    return results


async def get_plugin_errors_async(
    store: ConfigStore | None = None,
) -> list[str]:
    """Return errors encountered across marketplaces and plugins."""
    active_store = _get_active_store(store)
    if active_store is None:
        return []

    errors: list[str] = []
    records = await load_marketplace_records_async(active_store)
    for name, record in records.items():
        try:
            marketplace = load_marketplace_location(Path(record.install_location))
            errors.extend(f"{name}: {w}" for w in marketplace.warnings)
        except MarketplaceError as exc:
            errors.append(f"{name}: {exc}")

    # Check plugin load errors
    plugins_list = await active_store.list_plugins()
    for p in plugins_list:
        if p.get("load_error"):
            errors.append(f"{p['plugin_id']}: {p['load_error']}")

    return errors


# ── Synchronous Entrypoints & Legacy Compatibility ────────


class PluginDiscovery:
    """Legacy class wrapper for plugin discovery."""

    def __init__(self, project_root: Path | None = None, store: ConfigStore | None = None) -> None:
        self.project_root = project_root
        self._store = store

    def discover_all(self) -> dict[str, PluginInstance]:
        result = discover_plugins(self.project_root, store=self._store)
        return {p.name: p for p in result.plugins}


def discover_plugins(
    project_root: Path | None = None, store: ConfigStore | None = None
) -> PluginDiscoveryResult:
    """Synchronous discovery entrypoint."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run, discover_plugins_async(store, project_root)
                ).result()
        return asyncio.run(discover_plugins_async(store, project_root))
    except Exception as exc:
        logger.debug("discover_plugins async failed, falling back to local: %s", exc)
        # Fallback local project discovery
        plugins = []
        if project_root:
            proj_plugins = project_root / "plugins"
            if proj_plugins.is_dir():
                for p in sorted(proj_plugins.iterdir()):
                    if p.is_dir() and not p.name.startswith("."):
                        inst, _ = _plugin_from_install_path(
                            plugin_id=f"{p.name}@project",
                            root=p,
                            marketplace_name="project",
                            fallback_name=p.name,
                        )
                        if inst:
                            plugins.append(inst)
        return PluginDiscoveryResult(plugins=tuple(plugins), warnings=())
