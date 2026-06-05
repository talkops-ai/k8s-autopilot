import json
import sys
import asyncio
from pathlib import Path
import click
import uvicorn
from google.protobuf.json_format import ParseDict
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.responses import JSONResponse
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes, create_agent_card_routes, create_rest_routes
from a2a.server.tasks import (
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCard,
    AgentInterface,
)
from k8s_autopilot.config.config import Config
from k8s_autopilot.core import A2AAutoPilotExecutor
from k8s_autopilot.core.context_probe import ContextProbe
from k8s_autopilot.core.agents import (
    k8sAutopilotSupervisorAgent,
    create_k8sAutopilotSupervisorAgent,
    HelmOperatorCoordinator,
    K8sOperatorCoordinator,
    AppOperatorCoordinator,
    ObservabilityCoordinator,
)
from k8s_autopilot.utils.logger import AgentLogger, log_sync

# Create agent logger for server
server_logger = AgentLogger("k8sAutopilotServer")

@click.command()
@click.option('--host', 'host', default=None, help='Server host (default: from config)')
@click.option('--port', 'port', type=int, default=None, help='Server port (default: from config)')
@click.option('--agent-card', 'agent_card', default=None, help='Path to agent card JSON file (default: from config)')
@click.option('--config-file', 'config_file', help='Path to configuration file')
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
            )
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

        server_logger.info("Agent card loaded successfully",)

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
            coordinators=[helm_operator_coordinator, k8s_operator_coordinator, app_operator_coordinator, observability_coordinator]
        )

        # Verify supervisor is ready
        if not supervisor_agent.is_ready():
            raise RuntimeError("k8sAutopilotSupervisorAgent failed to initialize properly")

        server_logger.info("Custom Supervisor Agent initialized successfully", extra={
                "supervisor_name": supervisor_agent.name,
                "available_agents": supervisor_agent.list_agents(),
                "supervisor_ready": supervisor_agent.is_ready()
            }
        )

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
                "supervisor_agents": supervisor_agent.list_agents()
            }
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

        # Build Starlette app with CORS middleware for A2UI client access
        app = Starlette(routes=[
            *jsonrpc_routes,
            *agent_card_routes,
            *rest_routes,
            Route("/suggest-prompts", suggest_prompts_handler, methods=["GET"]),
        ])
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
            log_level=config.log_level.lower()
        )
    except FileNotFoundError as e:
        server_logger.error(f"File not found: {e}", extra={"error": str(e)}
        )
        sys.exit(1)
    except json.JSONDecodeError as e:
        server_logger.error(f"Invalid JSON in configuration file: {e}", extra={"error": str(e)}
        )
        sys.exit(1)
    except Exception as e:
        server_logger.error(f"An error occurred during server startup: {e}", extra={"error": str(e), "error_type": type(e).__name__}
        )
        sys.exit(1)


if __name__ == "__main__":
    main()