import json
import traceback
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Annotated
import re
from functools import wraps
from langchain_core.prompts import ChatPromptTemplate
from langgraph.types import Command, interrupt
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from pydantic import BaseModel
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent, CompiledSubAgent
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from typing import Dict, Any, Optional, Annotated, Callable, Awaitable
from k8s_autopilot.core.state.base import PlanningSwarmState
from k8s_autopilot.utils.logger import AgentLogger, log_async, log_sync
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from k8s_autopilot.core.agents.base_agent import BaseSubgraphAgent
from k8s_autopilot.core.agents.helm_generator.planner.tools.requirement_parser import (
    parse_requirements, 
    classify_complexity, 
    validate_requirements,
)
from k8s_autopilot.core.agents.helm_generator.planner.tools.planner import (
    analyze_application_requirements,
    design_kubernetes_architecture,
    estimate_resources,
    define_scaling_strategy,
    check_dependencies
)
from k8s_autopilot.core.agents.helm_generator.planner.planning_prompts import (
    PLANNING_SUPERVISOR_PROMPT,
    REQUIREMENT_ANALYZER_SUBAGENT_PROMPT,
    ARCHITECTURE_PLANNER_SUBAGENT_PROMPT
)
# Create agent logger for planning swarm    
planning_deep_agent_logger = AgentLogger("k8sAutopilotPlanningDeepAgent")


class PlanningStateMiddleware(AgentMiddleware):
    """
    Middleware to expose PlanningSwarmState to tools.
    
    This ensures all state fields (session_id, task_id, user_query, etc.) 
    are available in runtime.state for tools when using create_deep_agent.
    """
    state_schema = PlanningSwarmState
    # Tools that need access to the state
    tools = [
        parse_requirements,
        classify_complexity,
        validate_requirements,
        analyze_application_requirements,
        design_kubernetes_architecture,
        estimate_resources,
        define_scaling_strategy,
        check_dependencies
    ]

class ValidateRequirementsHITLMiddleware(AgentMiddleware):
    """
    Middleware to intercept validate_requirements tool output and trigger HITL if clarifications are needed.
    """
    
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        # Execute the tool first
        result = await handler(request)
        
        # Check if this is the validate_requirements tool
        if request.tool_call["name"] == "validate_requirements":
            # Check if result is a Command and has handoff_data
            if isinstance(result, Command) and result.update and "handoff_data" in result.update:
                handoff_data = result.update["handoff_data"]
                validation_result = handoff_data.get("validation_result", {})
                
                # Check if clarifications are needed
                clarifications = validation_result.get("clarifications_needed", [])
                if clarifications:
                    # Prepare interrupt payload
                    interrupt_payload = {
                        "pending_feedback_requests": {
                            "status": "input_required",
                            "session_id": request.runtime.state.get("session_id", "unknown"),
                            "question": "\n".join(clarifications),
                            "context": "Requirements validation identified missing information.",
                            "active_phase": "planning",
                            "tool_name": "validate_requirements",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    }
                    
                    planning_deep_agent_logger.log_structured(
                        level="INFO",
                        message="Triggering HITL for validation clarifications",
                        extra={"clarifications_count": len(clarifications)}
                    )
                    
                    # Trigger interrupt and wait for user feedback
                    user_feedback = interrupt(interrupt_payload)
                    
                    # Update the result with user feedback
                    if user_feedback:
                        # Append feedback to updated_user_requirements
                        current_requirements = request.runtime.state.get("updated_user_requirements", "")
                        new_requirements = f"{current_requirements}\n\nUser Feedback on Clarifications:\n{user_feedback}"
                        
                        # Update the Command to include the new requirements
                        result.update["updated_user_requirements"] = new_requirements
                        result.update["active_agent"] = "architecture_planner"
                        
                        # Log the update
                        planning_deep_agent_logger.log_structured(
                            level="INFO",
                            message="Updated requirements with user feedback",
                            extra={"feedback_length": len(str(user_feedback))}
                        )
                        
        return result

class k8sAutopilotPlanningDeepAgent(BaseSubgraphAgent):
    """
    Planning Deep Agent for K8s Autopilot which is used to plan the Helm chart dependencies and requirements.
    """
    
    def __init__(
        self,
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        name: str = "planner_sub_supervisor_agent",
        memory: Optional[MemorySaver] = None
    ):
        """
        Initialize the k8sAutopilotPlanningDeepAgent.
        Args:
            config: Configuration object for the planning deep agent.
            custom_config: Custom configuration for the planning deep agent.
            name: Name of the planning deep agent.
            memory: Memory/checkpointer instance for the planning deep agent.
        """
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="Initializing k8sAutopilotPlanningDeepAgent",
            extra={
                "config": config,
                "custom_config": custom_config,
                "name": name,
                "memory": memory
            }
        )        # Use centralized config system
        self.config_instance = config or Config(custom_config or {})
        
        # Set agent name for identification
        self._name = name
        self._planner_agent_state = PlanningSwarmState()
        self.memory = memory or MemorySaver()
        
        # Get LLM configuration from centralized config
        llm_config = self.config_instance.get_llm_config()
        
        try:
            self.model = LLMProvider.create_llm(
                provider=llm_config['provider'],
                model=llm_config['model'],
                temperature=llm_config['temperature'],
                max_tokens=llm_config['max_tokens']
            )
            planning_deep_agent_logger.log_structured(
                level="INFO",
                message=f"Initialized LLM model: {llm_config['provider']}:{llm_config['model']}",
                extra={"llm_provider": llm_config['provider'], "llm_model": llm_config['model']}
            )
        except Exception as e:
            planning_deep_agent_logger.log_structured(
                level="ERROR",
                message=f"Failed to initialize LLM model: {e}",
                extra={"error": str(e)}
            )
            raise

        self._initialize_sub_agents()
        self._define_planning_prompt()
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="k8sAutopilotPlanningDeepAgent initialized successfully",
            extra={
                "name": self._name,
                # "sub_agents": self._sub_agents,
                "planner_prompt_defined": hasattr(self, '_planner_prompt'),
            }
        )
        
    @property
    def name(self) -> str:
        """Agent name for Send() routing and identification."""
        return self._name
    
    @property
    def state_model(self) -> type[BaseModel]:
        """Pydantic model for agent's state schema."""
        return PlanningSwarmState
    
    def _initialize_sub_agents(self) -> None:
        """Initialize the 2 specialized subagents for planning phase."""
        
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="Initializing subagents for planning deep agent",
            extra={"agent_name": self._name}
        )

        # Subagent 1: Requirements Analyzer
        # Uses tools: parse_requirements, classify_complexity, validate_requirements
        self.requirement_analyzer_agent = create_agent(
            model=self.model,
            system_prompt=REQUIREMENT_ANALYZER_SUBAGENT_PROMPT,
            tools=[parse_requirements, classify_complexity, validate_requirements],
            state_schema=PlanningSwarmState,
            middleware=[ValidateRequirementsHITLMiddleware()],
        )
        self.requirements_analyzer_subagent = CompiledSubAgent(
            name="requirements_analyzer",
            description="Specialized agent for parsing, classifying, and validating Helm chart requirements. "
            "Use when you need to analyze user input and extract structured requirements. "
            "This agent will parse natural language into structured format, assess complexity level, "
            "and validate completeness of requirements.",
            runnable=self.requirement_analyzer_agent,
        )

        self.architecture_planner_agent = create_agent(
            model=self.model,
            system_prompt=ARCHITECTURE_PLANNER_SUBAGENT_PROMPT,
            tools=[analyze_application_requirements, design_kubernetes_architecture, estimate_resources, define_scaling_strategy, check_dependencies],
            state_schema=PlanningSwarmState,
        )
        self.architecture_planner_subagent = CompiledSubAgent(
            name="architecture_planner",
            description="Specialized agent for designing Kubernetes architecture, estimating resources, "
            "and defining scaling strategies. Use after requirements are validated. "
            "This agent will analyze application characteristics, design K8s resource structure, "
            "estimate CPU/memory sizing, define HPA configuration, and identify chart dependencies.",
            runnable=self.architecture_planner_agent,
        )
        self._sub_agents = [
            self.requirements_analyzer_subagent,
            self.architecture_planner_subagent
        ]
        
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="Subagents initialized successfully",
            extra={
                "subagent_count": len(self._sub_agents),
                "subagent_names": [sa["name"] for sa in self._sub_agents]
            }
        )

    def _define_planning_prompt(self) -> None:
        """Define the prompt for the main planning supervisor agent."""
        
        self._planner_prompt = PLANNING_SUPERVISOR_PROMPT
        
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="Planning supervisor prompt defined",
            extra={"prompt_length": len(self._planner_prompt)}
        )
    
    @tool
    def request_human_input(
        question: str,
        context: Optional[str] = None,
        phase: Optional[str] = None,
        runtime: ToolRuntime[None, PlanningSwarmState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = ""
    ) -> Command:
        """
        Request human input during workflow execution.
        
        **CRITICAL: This is the ONLY way to request human input. DO NOT write questions as text output.**
        
        This tool pauses execution and waits for human input. Use this when:
        - You need clarification on ambiguous requirements
        - You need approval for a decision
        - You need human input to proceed
        
        **IMPORTANT:** When requirements are unclear, you MUST call this tool. 
        Writing questions as text output will NOT work - the workflow will not pause.
        Only tool calls can trigger the human-in-the-loop interrupt.
        
        Args:
            question: The question or request for the human (can include multiple numbered points)
            context: Optional context about why feedback is needed
            phase: Optional current workflow phase
            runtime: Tool runtime for state access
            tool_call_id: Injected tool call ID from LangChain
            
        Returns:
            Command: Command to update state with human response
        """
        
        # Get current phase and session_id from state if runtime is available
        if runtime:
            if not phase:
                phase = runtime.state.get("active_phase", "planning")
            session_id = runtime.state.get("session_id", "unknown")
            task_id = runtime.state.get("task_id", "unknown")
        else:
            phase = phase or "planning"
            session_id = "unknown"
            task_id = "unknown"
        
        # Ensure tool_call_id has a value (should be injected, but provide fallback)
        if not tool_call_id:
            tool_call_id = "unknown"
        
        # Build interrupt payload
        interrupt_payload = {
            "pending_feedback_requests": {
                "status": "input_required",
                "session_id": session_id,
                "question": question,
                "context": context or "No additional context provided",
                "active_phase": phase or "planning",
                "tool_name": "request_human_input",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
        
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="Requesting human feedback via request_human_input tool",
            extra={
                "phase": phase,
                "question_preview": question[:100] if len(question) > 100 else question,
                "context": context,
                "session_id": session_id,
                "task_id": task_id
            }
        )
        
        # Pause execution - interrupt() returns human response
        # The interrupt payload is automatically available in __interrupt__ field
        # No need to store separately in state - it's redundant
        human_response = interrupt(interrupt_payload)
        
        # Convert human_response to string if needed
        human_response_str = str(human_response) if human_response else ""
        
        # Tool message must directly respond to the assistant's tool call
        # It should come first to satisfy LangChain's requirement that tool messages
        # immediately follow assistant messages with tool_calls
        tool_message_content = f"User has provided the additional requirements so proceed with the subagent call."
        tool_message = ToolMessage(content=tool_message_content, tool_call_id=tool_call_id)
        
        
        # Return Command to update state with human response
        # messages: Tool message (required to respond to tool call)
        # user_query: Updated with human response for context
        return Command(
            update={
                "updated_user_requirements": human_response_str,  # Update user query with human input
                "messages": [tool_message],  # Tool message to respond to tool call
                "question_asked": question,
            },
        )
    
    def build_graph(self) -> StateGraph:
        """
        Build the deep agent for planning phase.
        
        This creates a single deep agent with 2 specialized subagents:
        1. requirements_analyzer - parses and validates requirements
        2. architecture_planner - designs K8s architecture
        
        The main agent coordinates these subagents using the built-in task() tool.
        
        Returns:
            Compiled LangGraph agent ready for invocation
        """
        planning_deep_agent_logger.log_structured(
            level="INFO",
            message="Building planning deep agent graph",
            extra={"agent_name": self._name}
        )
        
        try:
            # Create the deep agent with subagents
            # CRITICAL: Use middleware with state_schema to ensure all state fields
            # are available in runtime.state for tools. context_schema alone is not enough.
            self.planning_agent = create_deep_agent(
                model=self.model,
                system_prompt=self._planner_prompt,
                tools=[self.request_human_input],  # HITL tool for requesting human input
                subagents=self._sub_agents,
                checkpointer=self.memory,
                context_schema=PlanningSwarmState,
                middleware=[PlanningStateMiddleware()],  # Middleware exposes state to tools
                # Note: For long-term memory across threads, add:
                # store=InMemoryStore(),
                # backend=lambda rt: CompositeBackend(
                #     default=StateBackend(rt),
                #     routes={"/memories/": StoreBackend(rt)}
                # )
            )
            
            planning_deep_agent_logger.log_structured(
                level="INFO",
                message="Planning deep agent built successfully",
                extra={
                    "agent_name": self._name,
                    "subagent_count": len(self._sub_agents),
                    "has_memory": True,
                    "has_checkpointer": self.memory is not None
                }
            )
            
            # Return the compiled agent graph
            return self.planning_agent
            
        except Exception as e:
            planning_deep_agent_logger.log_structured(
                level="ERROR",
                message=f"Failed to build planning deep agent: {e}",
                extra={
                    "error": str(e),
                    "agent_name": self._name
                }
            )
            raise

@log_sync
def create_planning_swarm_deep_agent(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "planning_swarm_deep_agent",
    memory: Optional[MemorySaver] = None
) -> k8sAutopilotPlanningDeepAgent:
    """
    Create a planning swarm deep agent.
    Args:
        config: Configuration object for the planning swarm deep agent.
        custom_config: Custom configuration for the planning swarm deep agent.
        name: Name of the planning swarm deep agent.
        memory: Memory/checkpointer instance for the planning swarm deep agent.
    Returns:
        k8sAutopilotPlanningDeepAgent: The planning swarm deep agent.
    """
    return k8sAutopilotPlanningDeepAgent(
        config=config,
        custom_config=custom_config,
        name=name,
        memory=memory
    )

def create_planning_swarm_deep_agent_factory(config: Config):
    """
    Factory function for creating planning swarm deep agents.
    Args:
        config: Configuration object for the planning swarm deep agent.
    Returns:
       Configured k8sAutopilotPlanningDeepAgent instance
    """
    return create_planning_swarm_deep_agent(config=config)
    