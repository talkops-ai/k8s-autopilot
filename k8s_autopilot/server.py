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
    create_k8sAutopilotSupervisorAgent,
    create_planning_swarm_deep_agent,
    create_template_supervisor,
    create_validator_deep_agent,
    create_helm_mgmt_deep_agent,
    create_argocd_onboarding_agent,
)
from k8s_autopilot.utils.logger import AgentLogger, log_sync

# Create agent logger for server
server_logger = AgentLogger("k8sAutopilotServer")

@click.command()
@click.option('--host', 'host', help='Server host')
@click.option('--port', 'port', type=int, help='Server port')
@click.option('--agent-card', 'agent_card', help='Path to agent card JSON file')
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
        server_logger.log_structured(
            level="INFO",
            message="Starting k8sAutopilot server",
            extra={"host": host, "port": port, "agent_card": agent_card, "config_file": config_file}
        )
        config = Config()
        if config_file:
            config.load_config(config_file)
            config = Config(config)
        
        host = host or config.a2a_server_host
        port = port or config.a2a_server_port

        if not agent_card:
            raise ValueError("Agent card is required")
            
        # Load agent card
        with Path(agent_card).open() as file:
            data = json.load(file)
        agent_card_obj: AgentCard = AgentCard(**data)

        server_logger.log_structured(
            level="INFO",
            message="Agent card loaded successfully",
        )

        # Create Planning Swarm Deep Agent
        planning_swarm_deep_agent = create_planning_swarm_deep_agent(config)
        
        # Create Template Supervisor (for Helm chart generation)
        template_supervisor = create_template_supervisor(
            config=config,
            name="template_supervisor"  # Must match the name expected in supervisor_agent.py
        )

        # Create Validator Deep Agent
        validator_deep_agent = create_validator_deep_agent(
            config=config,
            name="validator_deep_agent"  # Must match the name expected in supervisor_agent.py
        )

        # Create Helm Mgmt Deep Agent
        # Note: This factory is async because it initializes the MCP client.
        # We must run it synchronously here before the server loop starts.
        helm_mgmt_deep_agent = asyncio.run(create_helm_mgmt_deep_agent(
            config=config,
            name="helm_mgmt_deep_agent"
        ))

        # Create ArgoCD Onboarding Agent
        # Note: This factory is also async because it initializes its MCP client.
        argocd_onboarding_agent = asyncio.run(create_argocd_onboarding_agent(
            config=config,
            name="argocd_onboarding_deep_agent"  # Must match name in supervisor_agent.py
        ))

        # Create Supervisor Agent
        supervisor_agent = create_k8sAutopilotSupervisorAgent(
            agents=[planning_swarm_deep_agent, template_supervisor, validator_deep_agent, helm_mgmt_deep_agent, argocd_onboarding_agent],
            config=config,
            name="k8sAutopilotSupervisorAgent"
        )

        # Verify supervisor is ready
        if not supervisor_agent.is_ready():
            raise RuntimeError("k8sAutopilotSupervisorAgent failed to initialize properly")

        server_logger.log_structured(
            level="INFO",
            message="Custom Supervisor Agent initialized successfully",
            extra={
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
        
        server_logger.log_structured(
            level="INFO",
            message=f"Starting k8sAutopilot Server on {host}:{port}",
            extra={
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
        server_logger.log_structured(
            level="ERROR",
            message=f"File not found: {e}",
            extra={"error": str(e)}
        )
        sys.exit(1)
    except json.JSONDecodeError as e:
        server_logger.log_structured(
            level="ERROR",
            message=f"Invalid JSON in configuration file: {e}",
            extra={"error": str(e)}
        )
        sys.exit(1)
    except Exception as e:
        server_logger.log_structured(
            level="ERROR",
            message=f"An error occurred during server startup: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        sys.exit(1)


if __name__ == "__main__":
    main()