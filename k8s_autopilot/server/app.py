"""K8s Autopilot A2A Server entry point.

Wires the unified agent graph into the A2A JSONRPC, REST, and Thread routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging as _logging
import os
from pathlib import Path
import sys
from typing import Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard
import click
from google.protobuf.json_format import ParseDict  # type: ignore[import-untyped]
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.server.executor import A2AAutoPilotExecutor
from k8s_autopilot.utils.logger import configure_logging, get_logger, log_sync


class _TracerExceptionFilter(_logging.Filter):
    """Suppress 'No indexed run ID' messages from LangChain's callback manager."""

    def filter(self, record: _logging.LogRecord) -> bool:
        """Filter out log records containing 'No indexed run ID'.

        Args:
            record: The logging record being evaluated.

        Returns:
            bool: False if the record should be suppressed, True otherwise.
        """
        msg = record.getMessage()
        return "No indexed run ID" not in msg


_logging.getLogger("langchain_core.callbacks.manager").addFilter(_TracerExceptionFilter())

logger = get_logger(__name__)
server_logger = logger


def create_app(
    *,
    config_file: str | None = None,
    host: str | None = None,
    port: int | None = None,
    agent_card_path: str | None = None,
) -> Starlette:
    """Create the configured Starlette A2A application."""
    settings = get_settings()
    server_host = host or settings.a2a_server_host
    server_port = port or settings.a2a_server_port

    # Resolve agent card location (package-relative, project root, or cwd)
    card_file: Path | None = None
    if agent_card_path:
        p = Path(agent_card_path)
        if p.is_file():
            card_file = p

    if card_file is None:
        candidates = [
            Path(__file__).resolve().parent.parent / "card" / "k8s_autopilot.json",
            Path.cwd() / "k8s_autopilot" / "card" / "k8s_autopilot.json",
            Path.cwd() / "card" / "k8s_autopilot.json",
            Path.cwd() / "k8s_autopilot.json",
        ]
        if hasattr(settings, "project_root") and settings.project_root:
            candidates.insert(1, Path(settings.project_root) / "k8s_autopilot" / "card" / "k8s_autopilot.json")

        for candidate in candidates:
            if candidate.is_file():
                card_file = candidate
                break

    if card_file and card_file.is_file():
        logger.info("Loading agent card from %s", card_file)
        with card_file.open() as f:
            card_data = json.load(f)
    else:
        logger.warning("Agent card file not found, using fallback card definition")
        card_data = {
            "name": "k8s_autopilot",
            "description": "Production-grade multi-agent system for Kubernetes automation.",
            "version": "1.0.0",
            "supported_interfaces": [{"protocol_binding": "JSONRPC"}],
        }

    if server_host and server_port:
        dynamic_url = f"http://{server_host}:{server_port}"
        raw_interfaces = card_data.get("supported_interfaces")
        interfaces_list: list[Any] = (
            raw_interfaces if isinstance(raw_interfaces, list) else [{"protocol_binding": "JSONRPC"}]
        )
        card_data["supported_interfaces"] = [
            {**(iface if isinstance(iface, dict) else {}), "url": dynamic_url} for iface in interfaces_list
        ]

    agent_card_obj: AgentCard = ParseDict(card_data, AgentCard())

    # 1. Create A2A executor (checkpointer and agent graph wired during lifespan)
    executor = A2AAutoPilotExecutor()

    # 3. Create RequestHandler
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card_obj,
    )

    # 4. Route factories
    jsonrpc_routes = create_jsonrpc_routes(
        request_handler=request_handler,
        rpc_url="/",
        enable_v0_3_compat=True,
    )
    agent_card_routes = create_agent_card_routes(
        agent_card=agent_card_obj,
    )
    rest_routes = create_rest_routes(
        request_handler=request_handler,
        enable_v0_3_compat=True,
    )

    from k8s_autopilot.api.routes import create_thread_routes

    thread_routes = create_thread_routes()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        """Manage server lifecycle, store initialization, and credential sync.

        Args:
            app: Starlette application instance.

        Yields:
            None: Server operational context.
        """
        from k8s_autopilot.api.service import ThreadService, set_thread_service
        from k8s_autopilot.api.settings_routes import set_config_store
        from k8s_autopilot.config.store_factory import create_config_store

        # Initialize central logging configuration from active settings
        configure_logging()

        # Initialize ConfigStore (auto-detects Postgres vs SQLite)
        config_store = await create_config_store()
        await set_config_store(config_store)
        logger.info("ConfigStore initialized")

        # Bidirectional credentials sync between ConfigStore and .env / os.environ
        from k8s_autopilot.config.manifest import OptionKind, get_config_options
        from k8s_autopilot.config.paths import upsert_env_vars
        from k8s_autopilot.config.store import ConfigCategory

        env_sync: dict[str, str] = {}
        for option in get_config_options():
            if option.kind == OptionKind.SECRET or option.group == "Credentials":
                db_val = await config_store.get(option.db_key)
                env_val = os.environ.get(option.effective_env_var)
                if db_val and not env_val:
                    os.environ[option.effective_env_var] = db_val
                    env_sync[option.effective_env_var] = db_val
                elif env_val and not db_val:
                    await config_store.set(
                        key=option.db_key,
                        value=env_val,
                        category=ConfigCategory.SYSTEM,
                        is_secret=True,
                        display_name=option.summary,
                    )
        if env_sync:
            upsert_env_vars(env_sync)

        # Rehydrate all custom config entries into os.environ for runtime and subagents
        try:
            all_entries = await config_store.list_all()
            manifest_keys = {opt.db_key for opt in get_config_options()}
            manifest_keys.update({opt.key for opt in get_config_options()})
            custom_count = 0
            for entry in all_entries:
                if (
                    entry.key not in manifest_keys
                    and not entry.key.startswith("models.")
                    and not entry.key.startswith("credentials.")
                    and entry.value
                ):
                    os.environ[entry.key] = entry.value
                    custom_count += 1
            if custom_count > 0:
                logger.info("Rehydrated %d custom environment variables from store", custom_count)
        except Exception as exc:
            logger.warning("Error rehydrating custom config entries: %s", exc)

        # Hydrate Settings singleton from ConfigStore
        from k8s_autopilot.config.langsmith import apply_tracing_settings
        from k8s_autopilot.config.settings import reload_from_store
        from k8s_autopilot.model.config import (
            AVAILABLE_MODELS,
            PROVIDER_API_KEY_ENV,
            apply_stored_credentials,
        )

        hydrated_settings = await reload_from_store(config_store)
        logger.info("Settings rehydrated from store (model=%s)", hydrated_settings.model)

        for prov in sorted(set(AVAILABLE_MODELS.keys()) | set(PROVIDER_API_KEY_ENV.keys())):
            apply_stored_credentials(prov)

        tracing_enabled = apply_tracing_settings(hydrated_settings)
        if tracing_enabled:
            logger.info(
                "LangSmith tracing enabled for project: %s",
                hydrated_settings.langchain_project,
            )

        # Auto-rehydrate marketplaces and installed plugins from DB onto local cache
        try:
            from k8s_autopilot.plugins.discovery import discover_plugins_async

            rehydrated = await discover_plugins_async(store=config_store)
            logger.info(
                "Marketplaces & plugins auto-rehydrated: %d active plugins",
                len(rehydrated.plugins),
            )
        except Exception as exc:
            logger.warning("Plugin auto-rehydration during startup encountered warning: %s", exc)

        # Sync enabled MCP servers from DB into runtime session manager
        try:
            from k8s_autopilot.mcp.discovery import MCPDiscovery

            await MCPDiscovery(store=config_store).discover_and_sync_async(store=config_store)
            logger.info("MCP servers synced from ConfigStore to session manager")
        except Exception as exc:
            logger.warning("MCP server sync during startup encountered warning: %s", exc)

        # Initialize Persistent Checkpointer and Thread API
        from k8s_autopilot.state.session import create_checkpointer

        async with create_checkpointer() as persistent_checkpointer:
            executor.checkpointer = persistent_checkpointer
            thread_service = ThreadService(persistent_checkpointer, executor=executor)
            set_thread_service(thread_service)
            logger.info("Persistent checkpointer and Thread API initialized")

            # Warm up agent graph and preload MCP server tools at startup
            try:
                await asyncio.to_thread(executor._ensure_agent)
                logger.info("K8s Autopilot agent graph warmed up and ready")
            except Exception as exc:
                logger.warning("Agent warm-up during startup warning: %s", exc)

            yield

            # Clean up active MCP sessions on event loop before server shutdown
            try:
                from k8s_autopilot.mcp.session_manager import MCPSessionManager

                mgr = MCPSessionManager.get_instance()
                await mgr.cleanup()
            except Exception as e:
                logger.warning("Error cleaning up MCP sessions: %s", e)

        # Graceful force-exit on shutdown to bypass async queue deadlocks
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))

    async def health_handler(request: Any) -> JSONResponse:
        """GET /health — health check endpoint."""
        return JSONResponse({"status": "ok", "service": "k8s-autopilot"})

    from k8s_autopilot.api.settings_routes import create_settings_routes

    app_routes = [
        Route("/health", health_handler, methods=["GET"]),
        *create_settings_routes(settings),
        *thread_routes,
        *jsonrpc_routes,
        *agent_card_routes,
        *rest_routes,
    ]

    app = Starlette(routes=app_routes, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


@click.command()
@click.option("--host", "host", default=None, help="Server host")
@click.option("--port", "port", type=int, default=None, help="Server port")
@click.option("--agent-card", "agent_card", default=None, help="Path to agent card JSON")
@click.option("--config-file", "config_file", default=None, help="Path to config file")
@log_sync
def main(
    host: str | None,
    port: int | None,
    agent_card: str | None,
    config_file: str | None,
) -> None:
    """Start the K8s Autopilot server."""
    try:
        settings = get_settings()
        server_host = host or settings.a2a_server_host
        server_port = port or settings.a2a_server_port

        logger.info(
            "Starting K8s Autopilot server",
            extra={"host": server_host, "port": server_port},
        )

        app = create_app(
            config_file=config_file,
            host=server_host,
            port=server_port,
            agent_card_path=agent_card,
        )

        uvicorn.run(
            app,
            host=server_host,
            port=server_port,
            log_level=settings.log_level.lower(),
        )
    except Exception as e:
        logger.error("Error during server startup: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
