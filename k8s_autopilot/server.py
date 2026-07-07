import asyncio
import json

# ── Suppress LangChainTracer orphaned-run-ID noise ───────────────────────
# When the supervisor streams with subgraphs=True, the auto-injected
# LangChainTracer (from LANGCHAIN_TRACING_V2=true) receives on_*_end
# events from nested deep-agent subgraphs whose on_*_start it never saw.
# This produces harmless but noisy "No indexed run ID" TracerExceptions.
# The errors come from langchain_core.callbacks.manager via logger.warning().
import logging as _logging
import sys
from pathlib import Path

import click
import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import (
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCard,
)
from google.protobuf.json_format import ParseDict
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from k8s_autopilot.config.config import Config
from k8s_autopilot.core import A2AAutoPilotExecutor
from k8s_autopilot.core.agents import (
    AppOperatorCoordinator,
    HelmOperatorCoordinator,
    K8sOperatorCoordinator,
    ObservabilityCoordinator,
    create_k8sAutopilotSupervisorAgent,
)
from k8s_autopilot.core.context_probe import ContextProbe
from k8s_autopilot.utils.logger import AgentLogger, log_sync


class _TracerExceptionFilter(_logging.Filter):
    """Suppress 'No indexed run ID' messages from LangChain's callback manager."""
    def filter(self, record: _logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "No indexed run ID" in msg:
            return False
        return True

# Target the exact logger that emits these warnings
_logging.getLogger("langchain_core.callbacks.manager").addFilter(
    _TracerExceptionFilter(),
)

# Create agent logger for server
server_logger = AgentLogger("k8sAutopilotServer")

@click.command()
@click.option("--host", "host", default=None, help="Server host (default: from config)")
@click.option("--port", "port", type=int, default=None, help="Server port (default: from config)")
@click.option("--agent-card", "agent_card", default=None, help="Path to agent card JSON file (default: from config)")
@click.option("--config-file", "config_file", help="Path to configuration file")
@log_sync
def main(host: str, port: int, agent_card: str, config_file: str):
    """
    Start the k8sAutopilot server.
    Args:
        host: Server host
        port: Server port
        agent_card: Path to agent card JSON file
        config_file: Path to configuration file
    """
    try:
        server_logger.info("Starting k8sAutopilot server", extra={"host": host, "port": port, "agent_card": agent_card, "config_file": config_file})
        if config_file:
            config = Config.load_config(config_file)
        else:
            config = Config()

        host = host or config.a2a_server_host
        port = port or config.a2a_server_port

        agent_card_path: str = agent_card or str(
            config.get(
                "A2A_AGENT_CARD",
                "k8s_autopilot/card/k8s_autopilot.json",
            ),
        )

        # Load agent card (v1.0 Protobuf-based)
        with Path(agent_card_path).open() as file:
            data = json.load(file)

        # Inject dynamic URL into supported_interfaces
        if host and port:
            dynamic_url = f"http://{host}:{port}"
            data["supported_interfaces"] = [
                {**iface, "url": dynamic_url}
                for iface in data.get("supported_interfaces", [{"protocol_binding": "JSONRPC"}])
            ]

        agent_card_obj: AgentCard = ParseDict(data, AgentCard())

        server_logger.info("Agent card loaded successfully")

        # Create Helm Operator Coordinator
        helm_operator_coordinator = HelmOperatorCoordinator(config=config)

        # Create K8s Operator Coordinator
        k8s_operator_coordinator = K8sOperatorCoordinator(config=config)

        # Create App Operator Coordinator
        app_operator_coordinator = AppOperatorCoordinator(config=config)

        # Create Observability Coordinator
        observability_coordinator = ObservabilityCoordinator(config=config)

        # Create Supervisor Agent
        supervisor_agent = create_k8sAutopilotSupervisorAgent(
            config=config,
            name="k8sAutopilotSupervisorAgent",
            coordinators=[helm_operator_coordinator, k8s_operator_coordinator, app_operator_coordinator, observability_coordinator],
        )

        # Verify supervisor is ready
        if not supervisor_agent.is_ready():
            raise RuntimeError("k8sAutopilotSupervisorAgent failed to initialize properly")

        server_logger.info("Custom Supervisor Agent initialized successfully", extra={
                "supervisor_name": supervisor_agent.name,
                "available_agents": supervisor_agent.list_agents(),
                "supervisor_ready": supervisor_agent.is_ready(),
            },
        )

        # ── Check run mode ────────────────────────────────────────────
        run_mode = getattr(config, "AUTOPILOT_MODE", "a2a")

        if run_mode == "slack":
            from k8s_autopilot.core.integration.slack.app import create_slack_integration_server
            server_logger.info(f"Starting Slack integration server on {host}:{port}", extra={"host": host, "port": port})
            app = create_slack_integration_server(config, supervisor_agent)
        else:
            # Create A2AAutoPilotExecutor
            executor = A2AAutoPilotExecutor(agent=supervisor_agent)

            # Create RequestHandler (v1.0 — agent_card is now required)
            request_handler = DefaultRequestHandler(
                agent_executor=executor,
                task_store=InMemoryTaskStore(),
                agent_card=agent_card_obj,
            )

            # Create Starlette app with A2A v1.0 route factories
            jsonrpc_routes = create_jsonrpc_routes(
                request_handler=request_handler,
                rpc_url="/",
                enable_v0_3_compat=True,  # Support v0.3 clients
            )
            agent_card_routes = create_agent_card_routes(
                agent_card=agent_card_obj,
            )

            server_logger.info(f"Starting k8sAutopilot Server on {host}:{port}", extra={
                    "host": host,
                    "port": port,
                    "log_level": config.log_level,
                    "supervisor_agents": supervisor_agent.list_agents(),
                },
            )

            # ── Context Probe endpoint ────────────────────────────────
            context_probe = ContextProbe(config=config)

            async def suggest_prompts_handler(request):
                """GET /suggest-prompts — dynamic, cluster-aware prompt suggestions."""
                result = await context_probe.run()
                return JSONResponse(result)

            # ── A2A REST routes (protocol binding alongside JSONRPC) ──
            rest_routes = create_rest_routes(
                request_handler=request_handler,
                enable_v0_3_compat=True,
            )

            # ── Thread Management API ─────────────────────────────────
            from k8s_autopilot.api.routes import create_thread_routes
            thread_routes = create_thread_routes()

            import contextlib
            import os

            @contextlib.asynccontextmanager
            async def lifespan(app):
                # Startup Thread API backend
                from k8s_autopilot.api.service import ThreadService, set_thread_service
                from k8s_autopilot.core.hitl.checkpointer import (
                    create_async_checkpointer,
                    get_async_pool,
                    get_database_uri,
                )

                cp = await create_async_checkpointer(config)
                pool = get_async_pool()

                if pool and cp:
                    thread_service = ThreadService(cp)
                    set_thread_service(thread_service)
                    server_logger.info("Thread API backend initialized")
                else:
                    server_logger.warning("Thread API backend disabled (missing postgres pool)")

                # Initialize DB Settings table and start LISTEN/NOTIFY background task
                db_uri = get_database_uri(config)
                listener_task = None
                if db_uri:
                    try:
                        from k8s_autopilot.config.db_config import init_settings_table, start_settings_listener
                        await init_settings_table(db_uri)
                        listener_task = asyncio.create_task(start_settings_listener(db_uri, config))
                        server_logger.info("Settings listener background task started")
                    except Exception as e:
                        server_logger.error(f"Failed to initialize settings db/listener: {e}")

                # Upgrade checkpointer in supervisor_agent
                if hasattr(supervisor_agent, "_ensure_async_checkpointer"):
                    try:
                        await supervisor_agent._ensure_async_checkpointer()
                        upgraded_graph = (
                            getattr(supervisor_agent, "_graph", None)
                            or getattr(supervisor_agent, "graph", None)
                            or supervisor_agent
                        )
                        # Update Slack integration with the upgraded graph if in dual mode
                        if run_mode == "dual":
                            slack_integration = getattr(slack_app.state, "integration", None)
                            if slack_integration:
                                slack_integration.stream_bridge.graph = upgraded_graph
                                server_logger.info("Slack integration graph updated with AsyncPostgresSaver checkpointer")
                    except Exception as exc:
                        server_logger.warning(f"Failed to upgrade checkpointer in lifespan: {exc}")

                yield

                # Clean up listener task
                if listener_task:
                    server_logger.info("Stopping settings listener background task...")
                    listener_task.cancel()
                    try:
                        await listener_task
                    except asyncio.CancelledError:
                        pass

                # Close PostgreSQL checkpoint connection pool if active
                try:
                    from k8s_autopilot.core.hitl.checkpointer import (
                        close_async_pool,
                    )
                    await close_async_pool()
                except Exception as exc:  # noqa: BLE001
                    server_logger.warning(f"Pool cleanup error: {exc}")
                # On shutdown, uvicorn cancels all tasks. The a2a EventQueueSource catches
                # CancelledError in __aexit__ and deadlocks waiting for task_done().
                # We schedule a forceful exit after 500ms to bypass this hang.
                server_logger.info("Initiating graceful force-exit to bypass dispatcher deadlocks")
                asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))

            # Combine routes for dual/a2a modes
            from k8s_autopilot.api.settings_routes import create_settings_routes
            app_routes = [
                *create_settings_routes(config),
                *thread_routes,
                *jsonrpc_routes,
                *agent_card_routes,
                *rest_routes,
                Route("/suggest-prompts", suggest_prompts_handler, methods=["GET"]),
            ]

            if run_mode == "dual":
                from k8s_autopilot.core.integration.slack.app import create_slack_integration_server
                server_logger.info("Integrating Slack events and interactivity webhooks into A2A server (DUAL mode)")
                slack_app = create_slack_integration_server(config, supervisor_agent)
                slack_routes = [
                    r for r in slack_app.routes
                    if isinstance(r, Route) and r.path.startswith("/slack")
                ]
                # Prepend Slack routes to prevent shadowing by wildcard mounts (like /{tenant}) in rest_routes
                app_routes = slack_routes + app_routes

            # Build Starlette app with CORS middleware for A2UI client access
            app = Starlette(
                routes=app_routes,
                lifespan=lifespan,
            )
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=config.log_level.lower(),
        )
    except FileNotFoundError as e:
        server_logger.error(f"File not found: {e}", extra={"error": str(e)},
        )
        sys.exit(1)
    except json.JSONDecodeError as e:
        server_logger.error(f"Invalid JSON in configuration file: {e}", extra={"error": str(e)},
        )
        sys.exit(1)
    except Exception as e:
        server_logger.error(f"An error occurred during server startup: {e}", extra={"error": str(e), "error_type": type(e).__name__},
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
