"""Adapter from plugin and subagent MCP declarations to MCP configurations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k8s_autopilot.plugins.discovery import discover_plugins_async
from k8s_autopilot.plugins.substitution import plugin_environment, substitute_json

if TYPE_CHECKING:
    from k8s_autopilot.plugins.models import PluginInstance

logger = logging.getLogger(__name__)

_MCP_NAME_PART_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MCP_NAME_PART_LENGTH = 48


def _safe_mcp_name_part(value: str) -> str:
    """Sanitize string to safe MCP identifier part."""
    sanitized = _MCP_NAME_PART_RE.sub("_", value).strip("_")
    if sanitized == value and sanitized and len(sanitized) <= _MCP_NAME_PART_LENGTH:
        return sanitized
    digest = sha256(value.encode()).hexdigest()[:8]
    prefix = sanitized[:_MCP_NAME_PART_LENGTH] or "unnamed"
    return f"{prefix}_{digest}"


def scoped_mcp_server_name(plugin_id: str, server_name: str) -> str:
    """Namespace a plugin-declared MCP server's name under its plugin id.

    Args:
        plugin_id: Full plugin id in `name@marketplace` form.
        server_name: Unscoped server name from plugin config.

    Returns:
        Scoped server name safe for MCP session manager.
    """
    plugin_part = _safe_mcp_name_part(plugin_id)
    server_part = _safe_mcp_name_part(server_name)
    return f"plugin__{plugin_part}__{server_part}"


def scoped_subagent_mcp_server_name(subagent_name: str, server_name: str) -> str:
    """Namespace a subagent-declared MCP server under its subagent name.

    Args:
        subagent_name: Subagent identifier (e.g. 'helm-operator').
        server_name: Declared server name (e.g. 'talkops-helm-mcp-server').

    Returns:
        Scoped server name: `subagent__{subagent}__{server}`.
    """
    sub_part = _safe_mcp_name_part(subagent_name)
    server_part = _safe_mcp_name_part(server_name)
    return f"subagent__{sub_part}__{server_part}"


def _mcp_server_needs_login(server: object) -> bool:
    """Return whether an MCP server config typically requires interactive login."""
    if not isinstance(server, dict):
        return False
    server_type = server.get("type") or server.get("transport")
    if server_type in {"http", "sse"}:
        return True
    return isinstance(server.get("url"), str)


def _server_map(raw: object) -> dict[str, Any]:
    """Extract the server-name to config map from a decoded MCP document."""
    if not isinstance(raw, dict):
        return {}
    wrapped = raw.get("mcpServers")
    if isinstance(wrapped, dict):
        return dict(wrapped)
    codex_wrapped = raw.get("mcp_servers")
    if isinstance(codex_wrapped, dict):
        return dict(codex_wrapped)
    return dict(raw)


def _load_mcp_server_map(path: Path) -> dict[str, Any]:
    """Load an MCP config file and extract its server-name to config map."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping MCP config %s: %s", path, exc)
        return {}
    return _server_map(raw)


def _plugin_mcp_server_map(plugin: PluginInstance) -> dict[str, Any]:
    """Load a plugin's declared MCP servers without creating runtime state."""
    servers: dict[str, Any] = {}
    mcp_files = getattr(plugin.inventory, "mcp_files", ())
    for path in mcp_files:
        if path.suffix in {".mcpb", ".dxt"}:
            continue
        servers.update(_load_mcp_server_map(path))
    if plugin.manifest and getattr(plugin.manifest, "inline_mcp", None):
        servers.update(_server_map(plugin.manifest.inline_mcp))
    return servers


def plugin_mcp_server_entries(
    plugin: PluginInstance,
) -> tuple[tuple[str, str, bool], ...]:
    """List plugin MCP servers as `(label, scoped_name, needs_login)` tuples."""
    servers = _plugin_mcp_server_map(plugin)
    entries: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for name, server in servers.items():
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        entries.append(
            (
                name,
                scoped_mcp_server_name(plugin.plugin_id, name),
                _mcp_server_needs_login(server),
            ),
        )
    return tuple(entries)


def _normalize_server(
    server: object,
    *,
    root: Path,
    data_dir: Path | None = None,
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Normalize server dictionary and substitute path variables."""
    if not isinstance(server, dict):
        return {}
    substituted = substitute_json(
        dict(server),
        plugin_root=root,
        plugin_data=data_dir,
        project_dir=project_dir,
    )
    if isinstance(substituted, dict):
        cwd = substituted.get("cwd")
        if isinstance(cwd, str) and cwd and not Path(cwd).is_absolute():
            substituted["cwd"] = str((root / cwd).resolve())
        env = substituted.get("env")
        plugin_env = plugin_environment(
            plugin_root=root,
            plugin_data=data_dir,
            project_dir=project_dir,
        )
        if isinstance(env, dict):
            substituted["env"] = {**plugin_env, **env}
        else:
            substituted["env"] = plugin_env
        return substituted
    return {}


def plugin_mcp_configs(
    plugins: tuple[PluginInstance, ...] | list[PluginInstance],
    *,
    project_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Build MCP config layers for enabled plugins.

    Returns:
        List of server dicts with scoped server names.
    """
    configs: list[dict[str, Any]] = []
    for plugin in plugins:
        if plugin.data_dir is not None:
            try:
                plugin.data_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning(
                    "Could not create plugin data dir for %s", plugin.plugin_id,
                )
        servers = _plugin_mcp_server_map(plugin)
        scoped: dict[str, Any] = {}
        for server_name, server in servers.items():
            if not isinstance(server_name, str):
                continue
            scoped_name = scoped_mcp_server_name(plugin.plugin_id, server_name)
            scoped[scoped_name] = _normalize_server(
                server,
                root=plugin.root,
                data_dir=plugin.data_dir,
                project_dir=project_dir,
            )
            # Also register unscoped name if unique for direct lookup fallback
            scoped[server_name] = scoped[scoped_name]
        if scoped:
            configs.append(scoped)
    return configs


def subagent_mcp_configs(
    subagent_name: str,
    bundle_dir: Path,
    mcp_files: list[str | Path] | None = None,
    *,
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Build MCP configs for a built-in or custom subagent.

    Reads declared MCP server configuration files and returns a normalized
    server dictionary mapping both scoped and unscoped server names.

    Args:
        subagent_name: Subagent identifier (e.g., 'helm-operator').
        bundle_dir: Root directory of the subagent bundle.
        mcp_files: Optional list of MCP configuration file paths. If None,
            defaults to checking bundle_dir / "mcp.json".
        project_dir: Optional project directory for variable substitution.

    Returns:
        Normalized dictionary mapping server names to server configurations.
    """
    if mcp_files is None:
        target_files = [
            candidate
            for candidate in (bundle_dir / ".mcp.json", bundle_dir / "mcp.json")
            if candidate.is_file()
        ]
    else:
        target_files = [
            f if isinstance(f, Path) else (bundle_dir / f) for f in mcp_files
        ]

    normalized: dict[str, Any] = {}
    data_dir = bundle_dir / ".data"

    for file_path in target_files:
        raw_servers = _load_mcp_server_map(file_path)
        for name, server in raw_servers.items():
            scoped_name = scoped_subagent_mcp_server_name(subagent_name, name)
            norm_srv = _normalize_server(
                server,
                root=bundle_dir,
                data_dir=data_dir,
                project_dir=project_dir,
            )
            normalized[scoped_name] = norm_srv
            # Keep unscoped name mapped as well so both scoped and legacy calls resolve
            normalized[name] = norm_srv

    return normalized


def discover_plugin_mcp_configs(
    *,
    project_dir: Path | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """Discover enabled plugins and return merged MCP server configurations.

    Args:
        project_dir: Project directory for variable substitution.
        store: Optional ConfigStore.

    Returns:
        Dict mapping server names to normalized server configs.
    """
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    discover_plugins_async(store=store, project_root=project_dir),
                ).result()
        else:
            result = asyncio.run(
                discover_plugins_async(store=store, project_root=project_dir),
            )

        merged: dict[str, Any] = {}
        for layer in plugin_mcp_configs(result.plugins, project_dir=project_dir):
            merged.update(layer)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not discover plugin MCP configs: %s", exc)
        return {}
    else:
        return merged
