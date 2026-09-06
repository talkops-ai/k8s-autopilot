"""Discovers, synchronizes, and parses MCP server configs with Database as Source of Truth.

Features:
- Primary loading from DB ``mcp_servers`` table (honors ``enabled`` and ``trusted`` flags).
- Auto-seeding / synchronization from global, user, project, and plugin ``.mcp.json`` files.
- Full parity with OpsCode MCPServerConfig.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, TypedDict

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class MCPServerConfig(TypedDict, total=False):
    command: str | None
    args: list[str] | None
    env: dict[str, str] | None
    url: str | None
    transport: str | None
    headers: dict[str, str] | None
    source: str | None
    auth_token_env_var: str | None
    disabled_tools: list[str] | None
    allowed_tools: list[str] | None
    enabled: bool
    trusted: bool


def _load_mcp_json(path: Path) -> dict[str, dict[str, Any]]:
    """Safely load and parse an .mcp.json file."""
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            return servers
    except Exception as exc:
        logger.warning("Failed to parse MCP config at %s: %s", path, exc)
    return {}


class MCPDiscovery:
    """Discovers, syncs, and loads MCP servers with DB as source of truth."""

    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _get_store(self) -> Any:
        if self._store is None:
            try:
                from k8s_autopilot.api.settings_routes import _config_store
                if _config_store is not None:
                    self._store = _config_store
            except Exception:
                pass
        if self._store is None:
            try:
                from k8s_autopilot.config.store_factory import create_config_store_sync
                self._store = create_config_store_sync()
            except Exception as exc:
                logger.debug("Could not create fallback ConfigStore: %s", exc)
        return self._store

    async def discover_and_sync_async(
        self, project_root: Path | None = None, store: Any = None
    ) -> dict[str, MCPServerConfig]:
        """Discover filesystem configs, sync to DB, and return active enabled servers."""
        effective_project_root = project_root or Path.cwd()
        active_store = store or self._get_store()
        if active_store is not None and not getattr(active_store, "_initialized", False):
            try:
                await active_store.initialize()
            except Exception:
                pass

        candidates: list[tuple[Path, str]] = []
        from k8s_autopilot.config import paths

        # 1. Global user configs (~/.agents/)
        candidates.append((Path.home() / ".agents" / ".mcp.json", "global"))
        candidates.append((Path.home() / ".agents" / "mcp.json", "global"))

        # 2. User data dir configs (~/.k8s_autopilot/)
        candidates.append((paths.GLOBAL_MCP_PATH, "user"))
        candidates.append((paths.DATA_DIR / "mcp.json", "user"))

        # 3. Project configs (e.g. .mcp.json, mcp.json, .k8s_autopilot/.mcp.json)
        for proj_path in paths.project_mcp_paths(effective_project_root):
            candidates.append((proj_path, "project"))

        # 4. Local workspace plugins (.mcp.json in non-agent plugin root)
        plugins_dir = effective_project_root / "plugins"
        if plugins_dir.is_dir():
            for plugin in sorted(plugins_dir.iterdir()):
                if plugin.is_dir() and not plugin.name.startswith("."):
                    agents_dir = plugin / "agents"
                    if agents_dir.is_dir() and any(agents_dir.iterdir()):
                        continue  # Dynamic subagent plugins isolate their own MCP servers
                    candidates.append((plugin / ".mcp.json", f"plugin:{plugin.name}"))

        # 5. Installed plugins from DB (non-agent plugins only for global discovery)
        if active_store is not None:
            try:
                installed_plugins = await active_store.list_plugins()
                for p in installed_plugins:
                    if p.get("enabled", True) and p.get("install_path"):
                        inst_path = Path(p["install_path"])
                        if inst_path.is_dir():
                            agents_dir = inst_path / "agents"
                            if agents_dir.is_dir() and any(agents_dir.iterdir()):
                                continue  # Dynamic subagent plugins isolate their own MCP servers
                            candidates.append((inst_path / ".mcp.json", f"plugin:{p.get('name', inst_path.name)}"))
            except Exception as e:
                logger.debug("Error checking installed plugins for MCP discovery: %s", e)

        # Seed discovered bundle configs into DB if store is available
        if active_store is not None:
            # Purge legacy / incorrectly-seeded builtin subagent MCP servers from DB
            try:
                db_servers_all = await active_store.list_mcp_servers()
                for s in db_servers_all:
                    src = s.get("source", "")
                    if src and (src.startswith("builtin:") or src == "builtin"):
                        logger.info(
                            "Purging legacy subagent MCP server '%s' (source=%s) from global DB",
                            s.get("name"),
                            src,
                        )
                        await active_store.delete_mcp_server(s["name"])
            except Exception as exc:
                logger.debug("Failed purging legacy builtin MCP servers: %s", exc)

            existing_db_servers = {s["name"]: s for s in await active_store.list_mcp_servers()}
            for path, source_name in candidates:
                servers = _load_mcp_json(path)
                for name, config in servers.items():
                    if isinstance(config, dict) and name not in existing_db_servers:
                        url = config.get("url") or config.get("serverUrl")
                        command = config.get("command")
                        transport = config.get("transport") or config.get("type")
                        if not transport:
                            if url:
                                transport = "sse" if "sse" in str(url).lower() else "http"
                            else:
                                transport = "stdio"

                        enabled = True
                        if "disabled" in config:
                            enabled = not bool(config["disabled"])
                        elif "enabled" in config:
                            enabled = bool(config["enabled"])

                        disabled_tools = config.get("disabled_tools") or config.get("disabledTools") or []
                        allowed_tools = config.get("allowed_tools") or config.get("allowedTools") or []

                        srv_record = {
                            "name": name,
                            "transport": transport,
                            "command": command,
                            "args": config.get("args", []),
                            "url": url,
                            "env": config.get("env", {}),
                            "headers": config.get("headers", {}),
                            "source": source_name,
                            "auth_token_env_var": config.get("auth_token_env_var"),
                            "disabled_tools": disabled_tools,
                            "allowed_tools": allowed_tools,
                            "enabled": enabled,
                            "trusted": source_name.startswith("builtin"),
                        }
                        try:
                            await active_store.upsert_mcp_server(srv_record)
                        except Exception as e:
                            logger.debug("Could not auto-seed MCP server %s: %e", name, e)

            # Return all enabled servers from DB
            db_servers = await active_store.list_mcp_servers()
            result: dict[str, MCPServerConfig] = {}
            for s in db_servers:
                if s.get("enabled", True):
                    result[s["name"]] = MCPServerConfig(
                        command=s.get("command"),
                        args=s.get("args"),
                        env=s.get("env"),
                        url=s.get("url"),
                        transport=s.get("transport"),
                        headers=s.get("headers"),
                        source=s.get("source"),
                        auth_token_env_var=s.get("auth_token_env_var"),
                        disabled_tools=s.get("disabled_tools", []),
                        allowed_tools=s.get("allowed_tools", []),
                        enabled=s.get("enabled", True),
                        trusted=s.get("trusted", False),
                    )
            try:
                from k8s_autopilot.mcp.session_manager import MCPSessionManager

                MCPSessionManager.get_instance(result, purge_missing=False)
            except Exception as e:
                logger.debug("Failed to sync discovered configs to MCPSessionManager: %s", e)
            return result

        # Fallback to local file discovery if store is not available
        merged_servers: dict[str, MCPServerConfig] = {}
        for path, source_name in candidates:
            servers = _load_mcp_json(path)
            for name, config in servers.items():
                if isinstance(config, dict):
                    clean_config: MCPServerConfig = {
                        "command": config.get("command"),
                        "args": config.get("args"),
                        "env": config.get("env"),
                        "url": config.get("url"),
                        "transport": config.get("transport") or config.get("type"),
                        "headers": config.get("headers"),
                        "source": source_name,
                        "enabled": True,
                        "trusted": False,
                    }
                    merged_servers[name] = clean_config
        return merged_servers

    def discover(self, project_root: Path | None = None) -> dict[str, MCPServerConfig]:
        """Synchronous discover entrypoint."""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                # We are in an existing event loop, create task or fallback
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        asyncio.run, self.discover_and_sync_async(project_root)
                    ).result()
            return asyncio.run(self.discover_and_sync_async(project_root))
        except Exception:
            return self._discover_sync_fallback(project_root)

    def _discover_sync_fallback(self, project_root: Path | None = None) -> dict[str, MCPServerConfig]:
        from k8s_autopilot.config import paths

        effective_project_root = project_root or Path.cwd()
        merged_servers: dict[str, MCPServerConfig] = {}
        candidates: list[tuple[Path, str]] = [
            (Path.home() / ".agents" / ".mcp.json", "global"),
            (Path.home() / ".agents" / "mcp.json", "global"),
            (paths.GLOBAL_MCP_PATH, "user"),
            (paths.DATA_DIR / "mcp.json", "user"),
        ]
        for p in paths.project_mcp_paths(effective_project_root):
            candidates.append((p, "project"))

        plugins_dir = effective_project_root / "plugins"
        if plugins_dir.is_dir():
            for plugin in sorted(plugins_dir.iterdir()):
                if plugin.is_dir() and not plugin.name.startswith("."):
                    agents_dir = plugin / "agents"
                    if agents_dir.is_dir() and any(agents_dir.iterdir()):
                        continue  # Dynamic subagent plugins isolate their own MCP servers
                    candidates.append((plugin / ".mcp.json", f"plugin:{plugin.name}"))

        for path, source_name in candidates:
            servers = _load_mcp_json(path)
            for name, config in servers.items():
                if isinstance(config, dict):
                    merged_servers[name] = {
                        "command": config.get("command"),
                        "args": config.get("args"),
                        "env": config.get("env"),
                        "url": config.get("url"),
                        "transport": config.get("transport") or config.get("type"),
                        "headers": config.get("headers"),
                        "source": source_name,
                        "enabled": True,
                        "trusted": False,
                    }
        return merged_servers


def discover_mcp_configs(project_root: Path | None = None) -> dict[str, MCPServerConfig]:
    """Discover active MCP server configs."""
    return MCPDiscovery().discover(project_root)
