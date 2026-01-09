"""
Helm Chart Validator Deep Agent.

This module implements a Deep Agent for validating Helm charts using:
- Built-in file system tools (ls, read_file, write_file, edit_file) from DeepAgent
- Custom Helm validation tools (helm_lint_validator, helm_template_validator, helm_dry_run_validator)
- FilesystemBackend for real filesystem access (required for Helm commands)
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional, Callable, Awaitable
from pydantic import BaseModel
from langgraph.graph import StateGraph
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from k8s_autopilot.core.state.base import ValidationSwarmState, ValidationResult
from k8s_autopilot.utils.logger import AgentLogger, log_sync
from k8s_autopilot.config.config import Config
from langchain.chat_models import init_chat_model
from k8s_autopilot.core.agents.base_agent import BaseSubgraphAgent
from k8s_autopilot.core.agents.helm_generator.generator.tools.helm_validator_tools import (
    helm_lint_validator,
    helm_template_validator,
    helm_dry_run_validator,
    ask_human
)
from k8s_autopilot.core.agents.helm_generator.generator.generator_prompts import (
    VALIDATOR_SUPERVISOR_PROMPT
)

# Create agent logger for validator agent
validator_agent_logger = AgentLogger("k8sAutopilotValidatorDeepAgent")


class ValidationStateMiddleware(AgentMiddleware):
    """
    Middleware to expose ValidationSwarmState to tools.
    
    This ensures all state fields (generated_chart, chart_metadata, validation_results, etc.)
    are available in runtime.state for tools when using create_deep_agent.
    """
    state_schema = ValidationSwarmState
    # Tools that need access to the state
    tools = [
        helm_lint_validator,
        helm_template_validator,
        helm_dry_run_validator
    ]





class k8sAutopilotValidatorDeepAgent(BaseSubgraphAgent):
    """
    Validator Deep Agent for K8s Autopilot which validates Helm charts.
    
    This agent uses DeepAgent's built-in file system tools (ls, read_file, write_file, edit_file)
    along with custom Helm validation tools to comprehensively validate Helm charts.
    
    Features:
    - Built-in file system tools for chart file management
    - Custom Helm validation tools (lint, template, dry-run)
    - FilesystemBackend for real filesystem access (required for Helm commands)
    - Human-in-the-loop interrupts for critical validation failures
    - Automatic error fixing where possible
    - Comprehensive validation reporting
    """
    
    def __init__(
        self,
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        name: str = "validator_deep_agent",
        memory: Optional[MemorySaver] = None,
        workspace_dir: str = "/tmp/helm-charts"
    ):
        """
        Initialize the k8sAutopilotValidatorDeepAgent.
        
        Args:
            config: Configuration object for the validator agent
            custom_config: Custom configuration dictionary
            name: Name of the validator agent
            memory: Memory/checkpointer instance for the validator agent
            workspace_dir: Root directory for chart workspace (default: /tmp/helm-charts)
        """
        validator_agent_logger.log_structured(
            level="INFO",
            message="Initializing k8sAutopilotValidatorDeepAgent",
            extra={
                "config": config,
                "custom_config": custom_config,
                "name": name,
                "workspace_dir": workspace_dir
            }
        )
        
        # Use centralized config system
        self.config_instance = config or Config(custom_config or {})
        
        # Set agent name for identification
        self._name = name
        self._validator_agent_state = ValidationSwarmState()
        self.memory = memory or MemorySaver()
        self.workspace_dir = workspace_dir
        
        # Get LLM configuration from centralized config
        llm_config = self.config_instance.get_llm_config()
        
        try:
            # Remove 'provider' key as it's handled by model_provider or auto-inference
            config_for_init = {k: v for k, v in llm_config.items() if k != 'provider'}
            self.model = init_chat_model(**config_for_init)
            validator_agent_logger.log_structured(
                level="INFO",
                message=f"Initialized LLM model: {llm_config.get('provider', 'auto')}:{llm_config.get('model', 'unknown')}",
                extra={
                    "llm_model": llm_config.get('model', 'unknown')
                }
            )
            
            # Initialize Deep Agent model
            llm_deepagent_config = self.config_instance.get_llm_deepagent_config()
            deepagent_config_for_init = {k: v for k, v in llm_deepagent_config.items() if k != 'provider'}
            self.deep_agent_model = init_chat_model(**deepagent_config_for_init)
            validator_agent_logger.log_structured(
                level="INFO",
                message=f"Initialized Deep Agent LLM model: {llm_deepagent_config['provider']}:{llm_deepagent_config['model']}",
                extra={"llm_provider": llm_deepagent_config['provider'], "llm_model": llm_deepagent_config['model']}
            )
        except Exception as e:
            validator_agent_logger.log_structured(
                level="ERROR",
                message=f"Failed to initialize LLM model: {e}",
                extra={"error": str(e)}
            )
            raise
        
        self._define_validator_prompt()
        
        validator_agent_logger.log_structured(
            level="INFO",
            message="k8sAutopilotValidatorDeepAgent initialized successfully",
            extra={
                "name": self._name,
                "workspace_dir": self.workspace_dir,
                "validator_prompt_defined": hasattr(self, '_validator_prompt')
            }
        )
    
    @property
    def name(self) -> str:
        """Agent name for Send() routing and identification."""
        return self._name
    
    @property
    def state_model(self) -> type[BaseModel]:
        """Pydantic model for agent's state schema."""
        return ValidationSwarmState
    
    def _define_validator_prompt(self) -> None:
        """Define the prompt for the validator agent."""
        self._validator_prompt = VALIDATOR_SUPERVISOR_PROMPT
        
        validator_agent_logger.log_structured(
            level="INFO",
            message="Validator prompt defined",
            extra={"prompt_length": len(self._validator_prompt)}
        )
    
    def build_graph(self) -> StateGraph:
        """
        Build the deep agent for validation phase.
        
        This creates a Deep Agent with:
        - Built-in file system tools (ls, read_file, write_file, edit_file)
        - Custom Helm validation tools (helm_lint_validator, helm_template_validator, helm_dry_run_validator)
        - FilesystemBackend for real filesystem access
        
        The agent automatically:
        1. Writes chart files from state to workspace using built-in write_file
        2. Runs validations using custom Helm tools
        3. Fixes issues using built-in edit_file where possible
        4. Updates validation_results in state
        
        Returns:
            Compiled LangGraph agent ready for invocation
        """
        validator_agent_logger.log_structured(
            level="INFO",
            message="Building validator deep agent graph",
            extra={
                "agent_name": self._name,
                "workspace_dir": self.workspace_dir
            }
        )
        
        try:
            # Create the deep agent with FilesystemBackend for real filesystem access
            # This is required because Helm commands (helm lint, helm template, helm install --dry-run)
            # need access to real file paths on the filesystem.
            #
            # Built-in tools automatically available:
            # - ls: List files in workspace
            # - read_file: Read file contents
            # - write_file: Write new files
            # - edit_file: Edit existing files
            # - write_todos: Plan validation tasks
            #
            # Custom tools provided:
            # - helm_lint_validator: Fast syntax validation
            # - helm_template_validator: Template rendering validation
            # - helm_dry_run_validator: Cluster compatibility validation
            self.validator_agent = create_deep_agent(
                model=self.deep_agent_model,
                system_prompt=self._validator_prompt,
                tools=[
                    helm_lint_validator,
                    helm_template_validator,
                    helm_dry_run_validator,
                    ask_human
                    # Built-in tools (ls, read_file, write_file, edit_file) are automatically added
                ],
                checkpointer=self.memory,
                context_schema=ValidationSwarmState,
                middleware=[
                    ValidationStateMiddleware(),  # Middleware exposes state to tools
                ],
                backend=FilesystemBackend(root_dir=self.workspace_dir),  # Real filesystem access
                # Note: For long-term memory across threads, you can add:
                # store=InMemoryStore(),
                # backend=lambda rt: CompositeBackend(
                #     default=FilesystemBackend(root_dir=self.workspace_dir),
                #     routes={"/memories/": StoreBackend(rt)}
                # )
            )
            
            validator_agent_logger.log_structured(
                level="INFO",
                message="Validator deep agent built successfully",
                extra={
                    "agent_name": self._name,
                    "workspace_dir": self.workspace_dir,
                    "has_memory": True,
                    "has_checkpointer": self.memory is not None,
                    "backend_type": "FilesystemBackend"
                }
            )
            
            # Return the compiled agent graph
            return self.validator_agent
            
        except Exception as e:
            validator_agent_logger.log_structured(
                level="ERROR",
                message=f"Failed to build validator deep agent: {e}",
                extra={
                    "error": str(e),
                    "agent_name": self._name
                }
            )
            raise


@log_sync
def create_validator_deep_agent(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "validator_deep_agent",
    memory: Optional[MemorySaver] = None,
    workspace_dir: str = "/tmp/helm-charts"
) -> k8sAutopilotValidatorDeepAgent:
    """
    Create a validator deep agent.
    
    Args:
        config: Configuration object for the validator agent
        custom_config: Custom configuration dictionary
        name: Name of the validator agent
        memory: Memory/checkpointer instance for the validator agent
        workspace_dir: Root directory for chart workspace (default: /tmp/helm-charts)
    
    Returns:
        k8sAutopilotValidatorDeepAgent: The validator deep agent instance
    """
    return k8sAutopilotValidatorDeepAgent(
        config=config,
        custom_config=custom_config,
        name=name,
        memory=memory,
        workspace_dir=workspace_dir
    )


def create_validator_deep_agent_factory(config: Config, workspace_dir: str = "/tmp/helm-charts"):
    """
    Factory function for creating validator deep agents.
    
    Args:
        config: Configuration object for the validator agent
        workspace_dir: Root directory for chart workspace
    
    Returns:
        Configured k8sAutopilotValidatorDeepAgent instance
    """
    return create_validator_deep_agent(config=config, workspace_dir=workspace_dir)

