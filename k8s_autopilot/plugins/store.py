"""Database-backed state storage for K8s Autopilot plugin marketplaces and installs."""

from __future__ import annotations

from contextlib import suppress
from hashlib import sha256
import os
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from k8s_autopilot.plugins.models import (
    InstalledPluginEntry,
    InstallScope,
    MarketplaceRecord,
    MarketplaceSourceType,
    split_plugin_id,
)

if TYPE_CHECKING:
    from k8s_autopilot.config.store import ConfigStore

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_UNVERSIONED_CACHE_KEY = "unversioned"
_CACHE_SLUG_LENGTH = 48
_CACHE_DIGEST_LENGTH = 32
SUPPORTED_MARKETPLACE_SOURCE_TYPES: frozenset[MarketplaceSourceType] = frozenset(
    {"directory", "file", "github", "git", "url"}
)


def plugin_storage_root() -> Path:
    """Return the plugin storage root directory."""
    raw = os.environ.get("PLUGIN_CACHE_DIR") or os.environ.get("K8S_AUTOPILOT_PLUGIN_DIR")
    if raw:
        path = Path(raw).expanduser()
    else:
        from k8s_autopilot.config import paths

        path = paths.PLUGINS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_plugin_id(value: str) -> str:
    """Return a bounded, collision-resistant filesystem key."""
    slug = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {"_", "-"}) else "-" for ch in value)
    slug = slug.strip("-")[:_CACHE_SLUG_LENGTH] or "plugin"
    digest = sha256(value.encode()).hexdigest()[:_CACHE_DIGEST_LENGTH]
    return f"{slug}-{digest}"


def plugin_data_dir(plugin_id: str) -> Path:
    """Return the data directory path for a plugin id."""
    return plugin_storage_root() / "data" / sanitize_plugin_id(plugin_id)


def ensure_plugin_data_dir(plugin_id: str) -> Path:
    """Return the lazily-created data directory for a plugin id."""
    data_dir = plugin_data_dir(plugin_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def ensure_marketplace_cache_dir() -> Path:
    """Return the marketplace cache directory."""
    path = plugin_storage_root() / "marketplaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_plugin_install_cache_dir() -> Path:
    """Return the versioned plugin install cache root."""
    path = plugin_storage_root() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def versioned_cache_path(plugin_id: str, version: str | None) -> Path:
    """Return the versioned cache path for a plugin id."""
    plugin_name, marketplace = split_plugin_id(plugin_id)
    safe_version = sanitize_plugin_id(version or _UNVERSIONED_CACHE_KEY)
    return (
        ensure_plugin_install_cache_dir()
        / sanitize_plugin_id(marketplace)
        / sanitize_plugin_id(plugin_name)
        / safe_version
    )


def cache_and_register_plugin(
    plugin_id: str,
    source_dir: Path,
    *,
    version: str | None = None,
) -> Path:
    """Safely copy a plugin directory tree into the versioned cache."""
    cache_path = versioned_cache_path(plugin_id, version)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(tempfile.mkdtemp(prefix=f".{cache_path.name}.", dir=cache_path.parent))

    try:
        shutil.copytree(source_dir, temp_path, dirs_exist_ok=True)
        if cache_path.exists():
            shutil.rmtree(cache_path, ignore_errors=True)
        temp_path.replace(cache_path)
    except Exception:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise

    return cache_path


# ── DB-Backed Operations ──────────────────────────────────


async def load_marketplace_records_async(store: ConfigStore) -> dict[str, MarketplaceRecord]:
    """Load marketplace records from DB."""
    records_list = await store.list_marketplaces()
    results: dict[str, MarketplaceRecord] = {}
    for r in records_list:
        results[r["name"]] = MarketplaceRecord(
            name=r["name"],
            source_type=r.get("source_type", "directory"),
            source=r.get("source_value", ""),
            install_location=r.get("install_location", ""),
            ref=r.get("ref"),
            plugin_count=r.get("plugin_count", 0),
            is_team=bool(r.get("is_team", False)),
        )
    return results


async def save_marketplace_record_async(record: MarketplaceRecord, store: ConfigStore) -> None:
    """Save or update marketplace record in DB."""
    await store.upsert_marketplace(
        {
            "name": record.name,
            "source_type": record.source_type,
            "source_value": record.source,
            "install_location": record.install_location,
            "ref": record.ref,
            "plugin_count": record.plugin_count,
            "is_team": record.is_team,
        }
    )


async def delete_marketplace_record_async(name: str, store: ConfigStore) -> bool:
    """Delete marketplace record from DB."""
    return await store.delete_marketplace(name)


async def load_installed_plugins_async(store: ConfigStore) -> dict[str, InstalledPluginEntry]:
    """Load installed plugin entries from DB."""
    plugins_list = await store.list_plugins()
    results: dict[str, InstalledPluginEntry] = {}
    for p in plugins_list:
        if p.get("install_path"):
            results[p["plugin_id"]] = InstalledPluginEntry(
                install_path=p["install_path"],
                version=p.get("version"),
                scope=p.get("scope", "global"),
                installed_at=p.get("installed_at"),
                last_updated=p.get("last_updated"),
                git_commit_sha=p.get("git_commit_sha"),
            )
    return results


async def add_installed_plugin_entry_async(
    plugin_id: str,
    *,
    install_path: Path,
    version: str | None = None,
    scope: InstallScope = "global",
    display_name: str | None = None,
    description: str | None = None,
    author: Any = None,
    skill_count: int = 0,
    skill_names: list[str] | None = None,
    mcp_server_names: list[str] | None = None,
    source_type: str = "local",
    source_value: str | None = None,
    git_commit_sha: str | None = None,
    store: ConfigStore,
) -> InstalledPluginEntry:
    """Record an installed plugin in the database."""
    plugin_name, marketplace = split_plugin_id(plugin_id)
    entry = {
        "plugin_id": plugin_id,
        "name": plugin_name,
        "marketplace": marketplace,
        "version": version or "1.0.0",
        "display_name": display_name,
        "description": description,
        "author": author if isinstance(author, str) else (author.get("name") if isinstance(author, dict) else None),
        "skill_count": skill_count,
        "skill_names": skill_names or [],
        "mcp_server_names": mcp_server_names or [],
        "install_path": str(install_path.resolve()),
        "scope": scope,
        "source_type": source_type,
        "source_value": source_value or str(install_path.resolve()),
        "enabled": True,
        "git_commit_sha": git_commit_sha,
    }
    await store.upsert_plugin(entry)
    return InstalledPluginEntry(
        install_path=str(install_path.resolve()),
        version=version,
        scope=scope,
        git_commit_sha=git_commit_sha,
    )


async def remove_installed_plugin_entry_async(
    plugin_id: str,
    store: ConfigStore,
) -> bool:
    """Remove an installed plugin record from the database and remove cached directory."""
    plugin_record = await store.get_plugin(plugin_id)
    if plugin_record and plugin_record.get("install_path"):
        install_path = Path(plugin_record["install_path"])
        if install_path.exists() and "cache" in install_path.parts:
            with suppress(OSError):
                shutil.rmtree(install_path, ignore_errors=True)

    return await store.delete_plugin(plugin_id)


async def load_all_enabled_plugin_ids_async(store: ConfigStore) -> frozenset[str]:
    """Return all enabled plugin IDs from the database."""
    plugins_list = await store.list_plugins()
    return frozenset(p["plugin_id"] for p in plugins_list if p.get("enabled", True))


async def set_plugin_enabled_async(
    plugin_id: str,
    enabled: bool,
    store: ConfigStore,
) -> None:
    """Enable or disable a plugin in the database."""
    plugin = await store.get_plugin(plugin_id)
    if plugin:
        plugin["enabled"] = enabled
        await store.upsert_plugin(plugin)
    else:
        plugin_name, marketplace = split_plugin_id(plugin_id)
        await store.upsert_plugin(
            {
                "plugin_id": plugin_id,
                "name": plugin_name,
                "marketplace": marketplace,
                "enabled": enabled,
            }
        )
