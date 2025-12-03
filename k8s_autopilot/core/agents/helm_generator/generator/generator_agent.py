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
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from k8s_autopilot.core.state.base import ValidationSwarmState, ValidationResult
from k8s_autopilot.utils.logger import AgentLogger, log_sync
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from k8s_autopilot.core.agents.base_agent import BaseSubgraphAgent
from k8s_autopilot.core.agents.helm_generator.generator.tools.helm_validator_tools import (
    helm_lint_validator,
    helm_template_validator,
    helm_dry_run_validator
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


class ValidationHITLMiddleware(AgentMiddleware):
    """
    Middleware to intercept validation tool results and trigger Human-in-the-Loop interrupts
    for critical validation failures that require human approval.
    
    Interrupt points:
    1. Critical validation failures (severity="error" or "critical")
    2. When blocking_issues are detected
    3. Before final deployment approval (when deployment_ready would be set to true)
    """
    
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """
        Intercept tool calls and trigger interrupts for critical validation failures.
        
        Args:
            request: Tool call request from the agent
            handler: Handler to execute the tool call
        
        Returns:
            ToolMessage or Command with potential interrupt handling
        """
        # Execute the tool first
        result = await handler(request)
        
        # Check if this is a validation tool
        tool_name = request.tool_call.get("name", "")
        is_validation_tool = tool_name in [
            "helm_lint_validator",
            "helm_template_validator",
            "helm_dry_run_validator"
        ]
        
        if is_validation_tool and isinstance(result, Command):
            # Extract validation result from the Command update
            update = result.update or {}
            validation_results = update.get("validation_results", [])
            blocking_issues = update.get("blocking_issues", [])
            
            # Check for critical failures that need human approval
            critical_failures = []
            for validation_result in validation_results:
                # Handle both Pydantic model (ValidationResult) and dict formats
                if isinstance(validation_result, ValidationResult):
                    # Pydantic model instance
                    severity = validation_result.severity
                    passed = validation_result.passed
                    validator_name = validation_result.validator
                    message = validation_result.message
                    details = validation_result.details or {}
                elif isinstance(validation_result, dict):
                    # Dict format (serialized)
                    severity = validation_result.get("severity", "")
                    passed = validation_result.get("passed", True)
                    validator_name = validation_result.get("validator", "unknown")
                    message = validation_result.get("message", "Validation failed")
                    details = validation_result.get("details", {})
                elif hasattr(validation_result, "severity") and hasattr(validation_result, "passed"):
                    # Fallback for other BaseModel instances
                    severity = getattr(validation_result, "severity", "")
                    passed = getattr(validation_result, "passed", True)
                    validator_name = getattr(validation_result, "validator", "unknown")
                    message = getattr(validation_result, "message", "Validation failed")
                    details = getattr(validation_result, "details", {}) or {}
                else:
                    continue
                
                # Critical failures: error or critical severity, or not passed
                if not passed and severity in ["error", "critical"]:
                    critical_failures.append({
                        "validator": validator_name,
                        "severity": severity,
                        "message": message,
                        "details": details
                    })
            
            # Trigger interrupt if critical failures or blocking issues detected
            if critical_failures or blocking_issues:
                session_id = request.runtime.state.get("session_id", "unknown")
                task_id = request.runtime.state.get("task_id", "unknown")
                chart_metadata = request.runtime.state.get("chart_metadata", {})
                chart_name = chart_metadata.get("chart_name", "unknown") if isinstance(chart_metadata, dict) else "unknown"
                
                # Build interrupt question
                failure_summary = "\n".join([
                    f"- **{f['validator']}** ({f['severity']}): {f['message'][:200]}"
                    for f in critical_failures[:5]  # Limit to 5 failures
                ])
                
                blocking_summary = "\n".join([
                    f"- {issue[:200]}"
                    for issue in blocking_issues[:5]  # Limit to 5 issues
                ])
                
                question_parts = []
                if critical_failures:
                    question_parts.append(
                        f"**Critical Validation Failures Detected:**\n{failure_summary}"
                    )
                if blocking_issues:
                    question_parts.append(
                        f"**Blocking Issues:**\n{blocking_summary}"
                    )
                
                question = (
                    f"Helm chart validation has identified critical issues that require your review:\n\n"
                    f"{chr(10).join(question_parts)}\n\n"
                    f"**Chart:** {chart_name}\n\n"
                    f"How would you like to proceed?\n"
                    f"- **approve**: Proceed despite failures (not recommended)\n"
                    f"- **reject**: Stop validation and fix issues manually\n"
                    f"- **continue**: Continue validation (if non-critical)"
                )
                
                # Prepare interrupt payload
                interrupt_payload = {
                    "pending_feedback_requests": {
                        "status": "input_required",
                        "session_id": session_id,
                        "task_id": task_id,
                        "question": question,
                        "context": f"Validation tool '{tool_name}' detected critical failures requiring human approval.",
                        "active_phase": "validation",
                        "tool_name": tool_name,
                        "critical_failures": critical_failures,
                        "blocking_issues": blocking_issues,
                        "chart_name": chart_name,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                }
                
                validator_agent_logger.log_structured(
                    level="INFO",
                    message="Triggering HITL for critical validation failures",
                    extra={
                        "tool_name": tool_name,
                        "critical_failures_count": len(critical_failures),
                        "blocking_issues_count": len(blocking_issues),
                        "chart_name": chart_name,
                        "session_id": session_id
                    }
                )
                
                # Trigger interrupt and wait for user feedback
                user_feedback = interrupt(interrupt_payload)
                
                # Process user feedback
                if user_feedback:
                    feedback_str = str(user_feedback).lower().strip()
                    
                    validator_agent_logger.log_structured(
                        level="INFO",
                        message="Received user feedback for validation failures",
                        extra={
                            "tool_name": tool_name,
                            "feedback_preview": feedback_str[:100],
                            "session_id": session_id
                        }
                    )
                    
                    # Update the Command based on user decision
                    if "approve" in feedback_str or "proceed" in feedback_str:
                        # User approved - continue but mark as approved with warnings
                        result.update["human_approval_required"] = False
                        result.update["human_approved"] = True
                        result.update["approval_feedback"] = user_feedback
                        
                        # Add note to validation results about human approval
                        approval_note = {
                            "validator": "human_approval",
                            "passed": True,
                            "severity": "warning",
                            "message": f"Human approved proceeding despite critical failures: {user_feedback[:200]}",
                            "details": {
                                "approved_failures": critical_failures,
                                "approved_blocking_issues": blocking_issues
                            },
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        if "validation_results" not in result.update:
                            result.update["validation_results"] = []
                        result.update["validation_results"].append(approval_note)
                        
                    elif "reject" in feedback_str or "stop" in feedback_str:
                        # User rejected - stop validation
                        result.update["deployment_ready"] = False
                        result.update["human_approval_required"] = False
                        result.update["human_rejected"] = True
                        result.update["rejection_feedback"] = user_feedback
                        
                        # Add rejection note
                        rejection_note = {
                            "validator": "human_rejection",
                            "passed": False,
                            "severity": "critical",
                            "message": f"Human rejected deployment due to validation failures: {user_feedback[:200]}",
                            "details": {
                                "rejected_failures": critical_failures,
                                "rejection_reason": user_feedback
                            },
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        if "validation_results" not in result.update:
                            result.update["validation_results"] = []
                        result.update["validation_results"].append(rejection_note)
                        
                    else:
                        # Continue validation (default)
                        result.update["human_approval_required"] = False
                        result.update["approval_feedback"] = user_feedback
        
        return result


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
            self.model = LLMProvider.create_llm(
                provider=llm_config['provider'],
                model=llm_config['model'],
                temperature=llm_config['temperature'],
                max_tokens=llm_config['max_tokens']
            )
            validator_agent_logger.log_structured(
                level="INFO",
                message=f"Initialized LLM model: {llm_config['provider']}:{llm_config['model']}",
                extra={
                    "llm_provider": llm_config['provider'],
                    "llm_model": llm_config['model']
                }
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
                model=self.model,
                system_prompt=self._validator_prompt,
                tools=[
                    helm_lint_validator,
                    helm_template_validator,
                    helm_dry_run_validator
                    # Built-in tools (ls, read_file, write_file, edit_file) are automatically added
                ],
                checkpointer=self.memory,
                context_schema=ValidationSwarmState,
                middleware=[
                    ValidationStateMiddleware(),  # Middleware exposes state to tools
                    ValidationHITLMiddleware()   # Human-in-the-loop for critical failures
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

