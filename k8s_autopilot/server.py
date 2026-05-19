import json
import sys
import asyncio
from pathlib import Path
import click
import httpx
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCard,
)
from starlette.middleware.cors import CORSMiddleware
from k8s_autopilot.config.config import Config
from k8s_autopilot.core import A2AAutoPilotExecutor
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
        
        # Load agent card
        with Path(agent_card_path).open() as file:
            data = json.load(file)
        
        # Inject dynamic URL from host/port resolution mirroring aws_orchestrator
        if host and port:
            data["url"] = f"http://{host}:{port}"
            
        agent_card_obj: AgentCard = AgentCard(**data)

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
        
        # Create HTTP client
        client: httpx.AsyncClient = httpx.AsyncClient()

        # Create RequestHandler
        push_config_store = InMemoryPushNotificationConfigStore()
        push_sender = BasePushNotificationSender(
            httpx_client=client,
            config_store=push_config_store
        )

        request_handler = DefaultRequestHandler(
            agent_executor=executor, 
            task_store=InMemoryTaskStore(),
            push_config_store=push_config_store,
            push_sender=push_sender,
        )

        # Create A2AStarletteApplication
        server: A2AStarletteApplication = A2AStarletteApplication(
            agent_card=agent_card_obj,
            http_handler=request_handler,
        )
        
        server_logger.info(f"Starting k8sAutopilot Server on {host}:{port}", extra={
                "host": host, 
                "port": port, 
                "log_level": config.log_level,
                "supervisor_agents": supervisor_agent.list_agents()
            }
        )
        
        # Build app and add CORS middleware for A2UI client access
        app = server.build()
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