import uuid
import ast
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable, AsyncGenerator, Annotated, Tuple
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Send, Command, interrupt
from langgraph.checkpoint.memory import MemorySaver
import asyncio
from contextlib import asynccontextmanager
from .types import AgentResponse, BaseAgent
from k8s_autopilot.core.state.base import (
    MainSupervisorState,
    ChartRequirements,
    SupervisorWorkflowState,
    ApprovalStatus,
    WorkflowStatus
)
from k8s_autopilot.core.state.state_transformer import StateTransformer
from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime, InjectedToolCallId
from .base_agent import BaseSubgraphAgent
from k8s_autopilot.utils.logger import AgentLogger, log_async, log_sync
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider

# Create agent logger for supervisor
supervisor_logger = AgentLogger("k8sAutopilotSupervisorAgent")

@asynccontextmanager
async def isolation_shield():
    """Shield sub-supervisor from parent cancellation"""
    try:
        yield
    except asyncio.CancelledError:
        supervisor_logger.log_structured(
            level="WARNING",
            message="Parent cancelled, but allowing sub-supervisor to complete",
            extra={
                "current_task": str(asyncio.current_task()) if asyncio.current_task() else "No current task",
                "timestamp": datetime.now().isoformat()
            }
        )
        # Don't re-raise immediately, allow graceful completion
        await asyncio.sleep(0.1)  # Brief delay for cleanup
        raise

class k8sAutopilotSupervisorAgent(BaseAgent):
    """
    Supervisor agent that manages the overall workflow of the K8s Autopilot system.
    """
    @log_sync
    def __init__(
        self,
        agents: List[BaseSubgraphAgent],
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        prompt_template: Optional[str] = None,
        name: str = "supervisor-agent"
    ):
        """
        Initialize the k8sAutopilotSupervisorAgent.
        Args:
            agents: List of BaseSubgraphAgent instances to manage.
            config: Configuration object for the supervisor.
            custom_config: Custom configuration for the supervisor.
            prompt_template: Prompt template for the supervisor.
            name: Name of the supervisor agent.
        """
        # Use centralized config system
        self.config_instance = config or Config(custom_config or {})
        
        # Set agent name for identification
        self._name = name
        
        # Initialize memory for human-in-the-loop
        # Try to use HITL checkpointer (PostgreSQL if available, else MemorySaver)
        try:
            from k8s_autopilot.core.hitl import get_checkpointer
            self.memory = get_checkpointer(
                config=self.config_instance,
                prefer_postgres=True
            )
            supervisor_logger.log_structured(
                level="INFO",
                message="Initialized HITL checkpointer",
                extra={
                    "checkpointer_type": "postgres" if hasattr(self.memory, "setup") else "memory"
                }
            )
        except Exception as e:
            # Fallback to MemorySaver if HITL checkpointer fails
            self.memory = MemorySaver()
            supervisor_logger.log_structured(
                level="WARNING",
                message=f"Failed to initialize HITL checkpointer, using MemorySaver: {e}",
                extra={"error": str(e), "fallback": "MemorySaver"}
            )

        # Get LLM configuration from centralized config
        llm_config = self.config_instance.get_llm_config()

        # Initialize the LLM model using the centralized provider
        try:
            self.model = LLMProvider.create_llm(
                provider=llm_config['provider'],
                model=llm_config['model'],
                temperature=llm_config['temperature'],
                max_tokens=llm_config['max_tokens']
            )
            supervisor_logger.log_structured(
                level="INFO",
                message=f"Initialized LLM model: {llm_config['provider']}:{llm_config['model']}",
                extra={"llm_provider": llm_config['provider'], "llm_model": llm_config['model']}
            )
        except Exception as e:
            supervisor_logger.log_structured(
                level="ERROR",
                message=f"Failed to initialize LLM model: {e}",
                extra={"error": str(e)}
            )
            raise

        # Initialize agents and pass the checkpointer
        self.agents = {}
        for agent in agents:
            # Set the checkpointer for each agent
            if hasattr(agent, 'memory'):
                agent.memory = self.memory
            self.agents[agent.name] = agent
        # Set prompt template
        self.prompt_template = prompt_template or self._get_default_prompt()

        # Initialize supervisor state
        self.supervisor_state: Optional[MainSupervisorState] = None

        # Build the supervisor (create_agent returns compiled graph)
        self.compiled_graph = self._build_supervisor_graph()
        supervisor_logger.log_structured(
            level="DEBUG",
            message="Supervisor agent built successfully",
            extra={
                "compiled_graph_type": type(self.compiled_graph).__name__,
                "has_nodes": hasattr(self.compiled_graph, 'nodes')
            }
        )

    def _get_default_prompt(self) -> str:
        """Get the default prompt template for the supervisor following architecture pattern."""
        agent_names = list(self.agents.keys())
        agent_descriptions = "\n".join([f"- {name}: {self._get_agent_description(name)}" for name in agent_names])
        
        return f"""
You are a supervisor managing specialized swarms for Kubernetes Helm chart generation and deployment.

**IMPORTANT - REQUEST VALIDATION:**
You ONLY handle requests related to Helm chart generation, Kubernetes deployment, and CI/CD pipelines.

**VALID REQUEST EXAMPLES:**
- "Create a Helm chart for nginx"
- "Generate a Kubernetes deployment for my API"
- "Help me write a Helm chart for a web application"
- "Deploy my application to Kubernetes using Helm"
- "Create a Helm chart with PostgreSQL and Redis"
- "Set up CI/CD pipeline for Helm chart deployment"
- "Create ArgoCD application for my Helm chart"

**OUT-OF-SCOPE REQUEST HANDLING:**
If a user request is NOT clearly related to Helm chart generation, Kubernetes deployment, or CI/CD:
1. DO NOT reject immediately
2. Use the request_human_feedback tool to reach out to the user
3. Guide them about your capabilities
4. Only accept questions about your capabilities

**CAPABILITY GUIDANCE MESSAGE:**
When using request_human_feedback for out-of-scope requests, use this guidance:
"I can help you with:
- Creating and generating Helm charts for Kubernetes applications
- Designing Kubernetes deployment configurations
- Setting up CI/CD pipelines for Helm chart deployment
- Validating and securing Helm charts
- Deploying applications to Kubernetes clusters

Your request appears to be about [topic], which is outside my scope. Would you like help with Helm chart generation or Kubernetes deployment instead?"

**FEEDBACK TOOL USAGE:**
- request_human_feedback: Use this tool when you need to:
  * Clarify ambiguous requirements
  * Guide users about your capabilities for out-of-scope requests
  * Get approval for decisions
  * Request human input to proceed

Available tools:
- transfer_to_planning_swarm: Analyze requirements and create Helm chart architecture plans
- transfer_to_template_supervisor: Generate Helm chart templates and values files
- transfer_to_validator_deep_agent: Validate charts, perform security scanning, and prepare deployment configs
- request_human_feedback: Request human feedback, clarification, or guide users about capabilities

HITL APPROVAL GATES (REQUIRED):
- request_security_review: Request human review of security scan results (call after template_supervisor completes)
- request_deployment_approval: Request final approval before deployment (call after validator_deep_agent completes)

Your responsibilities:
1. **FIRST**: Validate that the request is Helm/Kubernetes/CI-CD related
   - If NOT related: Use request_human_feedback to guide user about your capabilities
   - Only proceed with workflow if user confirms Helm/Kubernetes/CI-CD related request
2. Analyze user requests and delegate to appropriate swarms using the transfer tools
3. Coordinate workflow through phases: planning → generation → validation
4. Enforce human-in-the-loop approval gates before phase transitions
5. Ensure final results meet user requirements

WORKFLOW SEQUENCE WITH HITL:
1. For ANY Helm chart request → transfer_to_planning_swarm(task_description="...")
2. When planning_complete → transfer_to_template_supervisor(task_description="...") [Proceeds automatically]
3. When generation_complete → request_security_review() [REQUIRED - workflow will pause for human approval]
4. When security_approved → transfer_to_validator_deep_agent(task_description="...")
5. When validation_complete → request_deployment_approval() [REQUIRED - workflow will pause for human approval]
6. When deployment_approved → Workflow complete

CRITICAL RULES:
- Check workflow_state flags before each tool call
- ALWAYS call HITL gate tools after phase completion (generation → request_security_review, validation → request_deployment_approval)
- Do NOT proceed to next phase without approval for security and deployment (check human_approval_status)
- If approval status is "pending" or "rejected", wait or end workflow
- Always call tools immediately, don't describe what you will do
- Do NOT do any chart generation/validation yourself - ONLY delegate using tools
- Template generation proceeds automatically after planning completes (no approval needed)

STOP CONDITIONS (When to finish):
- When deployment_approved == True AND validation_complete == True → Workflow is complete, respond with final summary
- When any phase is rejected AND user doesn't request changes → End workflow with error message
- When all phases are complete (planning_complete, generation_complete, validation_complete) → Respond with completion summary
- DO NOT keep calling tools if workflow is already complete - check workflow_state first

HITL GATE RULES:
- request_security_review: Call IMMEDIATELY after template_supervisor completes. Do NOT proceed to validation without approval.
- request_deployment_approval: Call IMMEDIATELY after validator_deep_agent completes. Do NOT complete workflow without approval.
- If gate returns "pending", workflow is paused - wait for human input
- If gate returns "approved", proceed to next phase
- If gate returns "rejected", end workflow or request changes
- Planning phase does NOT require approval - proceed directly to template generation

IMPORTANT: For requests like "help me write nginx helm chart", immediately call:
transfer_to_planning_swarm(task_description="create nginx helm chart")

Do not do any work yourself - only delegate using the transfer tools and HITL gates.
"""

    def _get_agent_description(self, agent_name: str) -> str:
        """Get description for an agent based on its name."""
        descriptions = {
            "planning_swarm": "Deep Agent swarm that validates requirements, researches best practices, and creates detailed Helm chart architecture plans",
            "template_supervisor": "Deep Agent swarm that generates Helm templates, values files, and documentation using file system tools",
            "validator_deep_agent": "Agent swarm that validates charts, performs security scanning, generates tests, and prepares ArgoCD deployment configurations"
        }
        return descriptions.get(agent_name, "Specialized Helm chart agent swarm")


    # ============================================================================
    # Tool Wrappers for create_agent() Pattern
    # ============================================================================
    
    def _create_swarm_tools(self, compiled_swarms: Dict[str, Any]) -> List:
        """
        Create tool wrappers for each swarm.
        
        These tools will be used by create_agent() instead of manual graph building.
        Each tool handles state transformation and subgraph invocation.
        """
        tools = []
        
        # Planning swarm tool
        if "planning_swarm_deep_agent" in compiled_swarms:
            planning_swarm = compiled_swarms["planning_swarm_deep_agent"]
            
            @tool
            async def transfer_to_planning_swarm(
                task_description: str,
                runtime: ToolRuntime[None, MainSupervisorState],
                tool_call_id: Annotated[str, InjectedToolCallId]
            ) -> Command:
                """
                Delegate to planning swarm for Helm chart architecture planning.
                
                Use this when:
                - User requests to create/write/generate Helm charts
                - Need to analyze requirements and create execution plans
                - workflow_state.planning_complete == False
                - active_phase == "requirements"
                """
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Planning swarm tool invoked",
                    extra={"task_description": task_description, "tool_call_id": tool_call_id}
                )
                
                # 1. Transform supervisor state → planning state
                planning_input = StateTransformer.supervisor_to_planning(runtime.state)
                
                # 2. Invoke planning swarm
                planning_output = await planning_swarm.ainvoke(
                    planning_input,
                    config={"recursion_limit": 100}
                )
                
                # 3. Transform back (note: state updates happen automatically via tool return)
                supervisor_updates = StateTransformer.planning_to_supervisor(
                    planning_output,
                    runtime.state,
                    tool_call_id
                )
                
                # Return Command with state updates
                return Command(
                    update=supervisor_updates
                )
            
            tools.append(transfer_to_planning_swarm)
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Created planning swarm tool",
                extra={"tool_name": "transfer_to_planning_swarm"}
            )
        
        # Generation swarm tool
        if "template_supervisor" in compiled_swarms:
            template_supervisor = compiled_swarms["template_supervisor"]
            
            @tool
            async def transfer_to_template_supervisor(
                task_description: str,
                runtime: ToolRuntime[None, MainSupervisorState],
                tool_call_id: Annotated[str, InjectedToolCallId]
            ) -> Command:
                """
                Delegate to generation swarm for Helm chart code generation.
                
                Use this when:
                - Planning is complete (workflow_state.planning_complete == True)
                - Need to generate actual Helm chart files
                - active_phase == "planning"
                """
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Generation swarm tool invoked",
                    extra={"task_description": task_description, "tool_call_id": tool_call_id}
                )
                
                # 1. Transform supervisor state → generation state
                generation_input = StateTransformer.supervisor_to_generation(runtime.state)
                
                # 2. Invoke generation swarm
                generation_output = await template_supervisor.ainvoke(
                    generation_input,
                    config={"recursion_limit": 100}
                )
                
                # 3. Transform back (note: state updates happen automatically via tool return)
                supervisor_updates = StateTransformer.generation_to_supervisor(
                    generation_output,
                    runtime.state,
                    tool_call_id
                )
                
                # Return Command with state updates
                return Command(
                    update=supervisor_updates
                )
            
            tools.append(transfer_to_template_supervisor)
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Created generation swarm tool",
                extra={"tool_name": "transfer_to_template_supervisor"}
            )
        
        # Validation swarm tool
        if "validator_deep_agent" in compiled_swarms:
            validator_deep_agent = compiled_swarms["validator_deep_agent"]
            
            @tool
            async def transfer_to_validator_deep_agent(
                task_description: str,
                runtime: ToolRuntime[None, MainSupervisorState]
            ) -> str:
                """
                Delegate to validation swarm for security and quality validation.
                
                Use this when:
                - Generation is complete (workflow_state.generation_complete == True)
                - Security approval granted (workflow_state.security_approved == True)
                - Need to validate generated Helm charts
                - active_phase == "generation"
                """
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Validation swarm tool invoked",
                    extra={"task_description": task_description}
                )
                
                # 1. Transform supervisor state → validation state
                # Get workspace_dir from state (set during security review), default to "/tmp/helm-charts"
                workspace_dir = runtime.state.get("workspace_dir", "/tmp/helm-charts")
                validation_input = StateTransformer.supervisor_to_validation(
                    runtime.state, 
                    workspace_dir=workspace_dir
                )
                
                # 2. Invoke validation swarm
                validation_output = await validator_deep_agent.ainvoke(
                    validation_input,
                    config={"recursion_limit": 100}
                )
                
                # 3. Transform back
                supervisor_updates = StateTransformer.validation_to_supervisor(
                    validation_output,
                    runtime.state
                )
                
                # Update the runtime state
                for key, value in supervisor_updates.items():
                    if key in runtime.state:
                        runtime.state[key] = value
                
                # Return meaningful result
                if supervisor_updates.get("workflow_state", {}).get("validation_complete"):
                    issue_count = len(supervisor_updates.get("validation_results", []))
                    return f"✅ Validation completed. Found {issue_count} validation results for: {task_description}"
                else:
                    return f"⏳ Validation in progress for: {task_description}"
            
            tools.append(transfer_to_validator_deep_agent)
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Created validation swarm tool",
                extra={"tool_name": "transfer_to_validator_deep_agent"}
            )
        
        return tools

    def _create_hitl_gate_tools(self) -> List:
        """
        Create HITL gate tools for human-in-the-loop approvals.
        
        Returns:
            List of HITL gate tool functions
        """
        from k8s_autopilot.core.hitl import create_hitl_gate_tools
        
        try:
            gate_tools = create_hitl_gate_tools()
            supervisor_logger.log_structured(
                level="INFO",
                message="Created HITL gate tools",
                extra={
                    "tool_count": len(gate_tools),
                    "tool_names": [t.name for t in gate_tools]
                }
            )
            return gate_tools
        except Exception as e:
            supervisor_logger.log_structured(
                level="WARNING",
                message=f"Failed to create HITL gate tools: {e}",
                extra={"error": str(e), "error_type": type(e).__name__}
            )
            return []

    def _check_approval_status(self, state: Dict[str, Any], phase: str) -> bool:
        """
        Check if a specific phase has been approved.
        
        Args:
            state: Current state dictionary
            phase: Phase to check ("planning", "security", "deployment")
            
        Returns:
            True if approved, False otherwise
        """
        from k8s_autopilot.core.hitl import is_approved
        return is_approved(state, phase)

    def _should_call_hitl_gate(self, state: Dict[str, Any], phase: str) -> bool:
        """
        Determine if HITL gate should be called for a phase.
        
        Args:
            state: Current state dictionary
            phase: Phase to check ("planning", "security", "deployment")
            
        Returns:
            True if gate should be called, False otherwise
        """
        # Check if already approved
        if self._check_approval_status(state, phase):
            return False
        
        # Check phase-specific conditions
        workflow_state = state.get("workflow_state", {})
        
        if phase == "planning":
            # Should call if planning is complete but not approved
            planning_complete = workflow_state.planning_complete if hasattr(workflow_state, "planning_complete") else workflow_state.get("planning_complete", False)
            planning_output = state.get("planning_output")
            return planning_complete and planning_output is not None
        
        elif phase == "security":
            # Should call if generation is complete and validation results exist
            generation_complete = workflow_state.generation_complete if hasattr(workflow_state, "generation_complete") else workflow_state.get("generation_complete", False)
            validation_results = state.get("validation_results", [])
            return generation_complete and len(validation_results) > 0
        
        elif phase == "deployment":
            # Should call if validation is complete
            validation_complete = workflow_state.validation_complete if hasattr(workflow_state, "validation_complete") else workflow_state.get("validation_complete", False)
            generated_artifacts = state.get("generated_artifacts", {})
            return validation_complete and len(generated_artifacts) > 0
        
        return False

    @tool
    def request_human_feedback(
        question: str,
        context: Optional[str] = None,
        phase: Optional[str] = None,
        runtime: ToolRuntime[None, MainSupervisorState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = ""
    ) -> Command:
        """
        Request human feedback during workflow execution.
        
        This tool pauses execution and waits for human input. Use this when:
        - You need clarification on ambiguous requirements
        - You need approval for a decision
        - You need human input to proceed
        
        Args:
            question: The question or request for the human
            context: Optional context about why feedback is needed
            phase: Optional current workflow phase
            runtime: Tool runtime for state access
            
        Returns:
            Command: Command to update state with human response
        """
        
        # Get current phase and session_id from state if runtime is available
        if runtime:
            if not phase:
                phase = runtime.state.get("active_phase", "unknown")
            session_id = runtime.state.get("session_id", "unknown")
        else:
            phase = phase or "unknown"
            session_id = "unknown"
        
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
                "active_phase": phase or "unknown"
            }
        }
        
        supervisor_logger.log_structured(
            level="INFO",
            message="Requesting human feedback",
            extra={
                "phase": phase,
                "question_preview": question[:100],
                "session_id": session_id
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
        tool_message_content = f"User has changed their mind. Now they want: {human_response_str}"
        tool_message = ToolMessage(content=tool_message_content, tool_call_id=tool_call_id)
        
        # Create human message for LLM input (separate from tool response)
        human_message = HumanMessage(content=human_response_str)
        
        # Return Command to update state with human response
        # messages: Only tool message (required to respond to tool call)
        # llm_input_messages: Human message (for LLM processing)
        return Command(
            update={
                "user_query": human_response_str,
                "messages": [tool_message],  # Only tool message to respond to tool call
                "llm_input_messages": [human_message],  # Human message for LLM input
            },
        )

    def _handle_hitl_interrupt(
        self,
        item: Dict[str, Any],
        context_id: str,
        task_id: str,
        step_count: int
    ) -> Optional[AgentResponse]:
        """
        Handle HITL interrupt and format response for client.
        
        Supports multiple interrupt types:
        - request_human_feedback: General feedback requests from tools
        - hitl_gate: Phase-level approval gates (planning, security, deployment)
        - generic: Unknown interrupt types
        
        Args:
            item: State item containing __interrupt__ field
            context_id: Session/context identifier
            task_id: Task identifier
            step_count: Current step count
            
        Returns:
            AgentResponse for HITL interrupt, or None if not a valid interrupt
        """
        try:
            # Extract interrupt data
            interrupt_list = item.get('__interrupt__', [])
            if not interrupt_list:
                return None
            
            # Get interrupt payload (first interrupt in list)
            interrupt_payload = interrupt_list[0].value if hasattr(interrupt_list[0], 'value') else interrupt_list[0]
            
            if not isinstance(interrupt_payload, dict):
                supervisor_logger.log_structured(
                    level="WARNING",
                    message="Interrupt payload is not a dict",
                    extra={
                        "payload_type": type(interrupt_payload).__name__,
                        "task_id": task_id
                    }
                )
                interrupt_payload = {"type": "generic", "data": str(interrupt_payload)}
            
            # Detect interrupt type and handle accordingly
            if "pending_feedback_requests" in interrupt_payload:
                # Handle feedback request interrupt from request_human_feedback tool
                feedback_data = interrupt_payload.get("pending_feedback_requests", {})
                content = {
                    'type': 'human_feedback_request',
                    'question': feedback_data.get('question', 'Input required'),
                    'context': feedback_data.get('context', ''),
                    'phase': feedback_data.get('active_phase', 'unknown'),
                    'status': feedback_data.get('status', 'input_required'),
                    'session_id': feedback_data.get('session_id', context_id)
                }
                
                interrupt_metadata = {
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'human_feedback_request',
                    'interrupt_type': 'human_feedback',
                    'phase': feedback_data.get('active_phase', 'unknown'),
                    'interrupt_data': feedback_data
                }
                
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Human feedback request interrupt detected",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        'phase': feedback_data.get('active_phase', 'unknown'),
                        'question_preview': feedback_data.get('question', '')[:100]
                    }
                )
                
            elif "tool_call_results_for_review" in interrupt_payload:
                # Handle tool call result review interrupt (Use Case 2)
                tool_review_data = interrupt_payload.get("tool_call_results_for_review", {})
                
                # Extract tool call information
                tool_call_id = tool_review_data.get("tool_call_id", "unknown")
                tool_name = tool_review_data.get("tool_name", "unknown")
                tool_args = tool_review_data.get("tool_args", {})
                tool_result = tool_review_data.get("tool_result")
                phase = tool_review_data.get("phase", "unknown")
                requires_review = tool_review_data.get("requires_review", True)
                review_status = tool_review_data.get("review_status", "pending")
                
                content = {
                    'type': 'tool_result_review_request',
                    'tool_call_id': tool_call_id,
                    'tool_name': tool_name,
                    'tool_args': tool_args,
                    'tool_result': tool_result,
                    'phase': phase,
                    'requires_review': requires_review,
                    'review_status': review_status,
                    'session_id': tool_review_data.get('session_id', context_id)
                }
                
                interrupt_metadata = {
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'tool_result_review_request',
                    'interrupt_type': 'tool_result_review',
                    'phase': phase,
                    'tool_call_id': tool_call_id,
                    'tool_name': tool_name,
                    'interrupt_data': tool_review_data
                }
                
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Tool call result review request interrupt detected",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        'phase': phase,
                        'tool_name': tool_name,
                        'tool_call_id': tool_call_id,
                        'review_status': review_status
                    }
                )
                
            elif "pending_tool_calls" in interrupt_payload:
                # Handle critical tool call pre-approval interrupt (Use Case 4)
                tool_call_data = interrupt_payload.get("pending_tool_calls", {})
                
                # Extract tool call information
                tool_call_id = tool_call_data.get("tool_call_id", "unknown")
                tool_name = tool_call_data.get("tool_name", "unknown")
                tool_args = tool_call_data.get("tool_args", {})
                is_critical = tool_call_data.get("is_critical", False)
                phase = tool_call_data.get("phase", "unknown")
                reason = tool_call_data.get("reason", "Approval required for critical operation")
                status = tool_call_data.get("status", "pending")
                
                content = {
                    'type': 'tool_call_approval_request',
                    'tool_call_id': tool_call_id,
                    'tool_name': tool_name,
                    'tool_args': tool_args,
                    'is_critical': is_critical,
                    'phase': phase,
                    'reason': reason,
                    'status': status,
                    'session_id': tool_call_data.get('session_id', context_id)
                }
                
                interrupt_metadata = {
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'tool_call_approval_request',
                    'interrupt_type': 'critical_tool_call_approval',
                    'phase': phase,
                    'tool_call_id': tool_call_id,
                    'tool_name': tool_name,
                    'is_critical': is_critical,
                    'reason': reason,
                    'interrupt_data': tool_call_data
                }
                
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Critical tool call approval request interrupt detected",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        'phase': phase,
                        'tool_name': tool_name,
                        'tool_call_id': tool_call_id,
                        'is_critical': is_critical,
                        'status': status,
                        'reason': reason[:100] if reason else 'No reason provided'
                    }
                )
                
            else:
                # Generic/unknown interrupt type
                content = {
                    'type': 'generic_interrupt',
                    'message': interrupt_payload.get('message', 'Human input required'),
                    'data': interrupt_payload
                }
                
                interrupt_metadata = {
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'generic_interrupt',
                    'interrupt_type': 'generic',
                    'interrupt_data': interrupt_payload
                }
                
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Generic interrupt detected",
                    task_id=task_id,
                    context_id=context_id,
                    extra={'payload_keys': list(interrupt_payload.keys())}
                )
            
            # Return unified AgentResponse
            return AgentResponse(
                response_type='human_input',
                is_task_complete=False,
                require_user_input=True,
                content=content,
                metadata=interrupt_metadata
            )
            
        except Exception as e:
            supervisor_logger.log_structured(
                level="ERROR",
                message=f"Error handling HITL interrupt: {e}",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__
                }
            )
            # Fallback to basic interrupt response
            return AgentResponse(
                response_type='human_input',
                is_task_complete=False,
                require_user_input=True,
                content="Human review required. Please provide your decision.",
                metadata={
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'input_required',
                    'error': str(e)
                }
            )

    def _build_supervisor_graph(self):
        """
        Build the supervisor using create_agent() with tool wrappers.
        
        This replaces the manual StateGraph building with the higher-level
        create_agent() API that uses tools for delegation.
        
        Pattern: "Tool-based delegation" (similar to custom_agents.py demo)
        - Each swarm is wrapped in a @tool function
        - Tools handle state transformation (supervisor ↔ swarm)
        - LLM decides which tool to call based on workflow state
        - No manual routing or conditional edges needed
        """
    
        # Build compiled subgraphs for each swarm
        compiled_swarms = {}
        for agent_name, agent in self.agents.items():
            graph = agent.build_graph()
            
            # Check if graph is already compiled (e.g., from create_deep_agent)
            if hasattr(graph, 'compile'):
                # Graph is uncompiled StateGraph, compile it
                compiled_graph = graph.compile(
                    name=agent_name  # Set the agent name for tracing
                )
            else:
                # Graph is already compiled (e.g., CompiledStateGraph from create_deep_agent)
                compiled_graph = graph
            
            compiled_swarms[agent_name] = compiled_graph
            
            supervisor_logger.log_structured(
                level="DEBUG",
                message=f"Compiled {agent_name} subgraph",
                extra={
                    "agent_name": agent_name,
                    "was_already_compiled": not hasattr(graph, 'compile'),
                    "has_nodes": hasattr(compiled_graph, 'nodes'),
                    "node_count": len(compiled_graph.nodes) if hasattr(compiled_graph, 'nodes') else 0
                }
            )
        
        # Create tool wrappers for delegation (replaces manual graph building)
        swarm_tools = self._create_swarm_tools(compiled_swarms)
        
        # # Create HITL gate tools
        # hitl_gate_tools = self._create_hitl_gate_tools()
        
        # Create human feedback tool
        feedback_tool = self.request_human_feedback
        
        # Combine all tools
        all_tools = swarm_tools + [feedback_tool]
        
        supervisor_logger.log_structured(
            level="DEBUG",
            message="Created tool wrappers",
            extra={
                "swarm_tool_count": len(swarm_tools),
                "total_tool_count": len(all_tools),
                "swarm_tool_names": [t.name for t in swarm_tools],
            }
        )
        
        # Use create_agent() instead of create_supervisor()
        # This eliminates ~200 lines of manual graph building
        # Note: recursion_limit is set in the config when invoking the graph, not here
        supervisor_agent = create_agent(
            model=self.model,
            tools=all_tools,  # Tool wrappers with state transformation + HITL gates
            system_prompt=self.prompt_template,
            state_schema=MainSupervisorState,
            checkpointer=self.memory
        )
        
        supervisor_logger.log_structured(
            level="INFO",
            message="Created supervisor using create_agent() with tool wrappers",
            extra={
                "tool_count": len(swarm_tools),
                "tool_names": [t.name for t in swarm_tools],
                "uses_checkpointer": self.memory is not None
            }
        )
        
        return supervisor_agent

    @property
    def name(self) -> str:
        """Get the name of the supervisor agent."""
        return self._name

    def _validate_planner_agent_completion(self, state: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        """
        Validate that the planner agent has completed successfully.
        
        Args:
            state: Current state dictionary
            
        Returns:
            Tuple of (is_complete: bool, planning_status: dict)
        """
        planning_status = {
            'data_values': {},
            'completion_metrics': {},
            'planning_state': {},
            'validation_result': 'PASS'
        }
        
        # Check if planning_output exists in state
        planning_output = state.get('planning_output')
        if not planning_output:
            return False, planning_status
        
        # Extract planning data
        if isinstance(planning_output, dict):
            planning_status['data_values'] = {
                'requirements_data': planning_output.get('requirements', ''),
                'execution_data': planning_output.get('chart_plan', '')
            }
            planning_status['planning_state'] = planning_output
        
        # Check workflow state
        workflow_state = state.get('workflow_state')
        if workflow_state:
            planning_status['completion_metrics'] = {
                'planning_complete': getattr(workflow_state, 'planning_complete', False),
                'phase': getattr(workflow_state, 'current_phase', 'unknown')
            }
            
            is_complete = getattr(workflow_state, 'planning_complete', False)
        else:
            is_complete = False
        
        return is_complete, planning_status

    def _create_initial_state(
        self,
        user_query: str,
        context_id: str,
        task_id: str
    ) -> MainSupervisorState:
        """
        Create initial supervisor state with all required Pydantic models and defaults.
        
        Args:
            user_query: The user's initial query/requirements
            context_id: Session/context identifier
            task_id: Task identifier
            
        Returns:
            MainSupervisorState: Fully initialized state dict
        """
        # Create initial message with metadata
        initial_message = HumanMessage(
            content=user_query,
            additional_kwargs={
                "session_id": context_id,
                "task_id": task_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        )
        
        # Initialize ChartRequirements from user query
        user_query = user_query
        
        # Initialize SupervisorWorkflowState
        workflow_state = SupervisorWorkflowState(
            workflow_id=task_id,
            current_phase="requirements",
            started_at=datetime.now(timezone.utc)
        )
        
        # Initialize HITL approval tracking
        human_approval_status = {
            "planning": ApprovalStatus(status="pending"),
            "security": ApprovalStatus(status="pending"),
            "deployment": ApprovalStatus(status="pending")
        }
        
        # Return fully initialized state
        state: MainSupervisorState = {
            "messages": [initial_message],
            "llm_input_messages": [initial_message],
            "user_query": user_query,
            "active_phase": "requirements",
            "workflow_state": workflow_state,
            "status": WorkflowStatus.PENDING,  # Use enum instead of string
            "human_approval_status": human_approval_status,
            "validation_results": [],
            "file_artifacts": {},
            "todos": [],
            "session_id": context_id,
            "task_id": task_id
        }
        
        return state

    @log_async
    async def stream(
        self,
        query_or_command,
        context_id: str,
        task_id: str
    ) -> AsyncGenerator[AgentResponse, None]:
        """
        Simplified async stream method following the reference pattern.
        
        Features:
        - Simple state management using StateTransformer
        - Clean interrupt detection with '__interrupt__' key
        - Direct status-based response formatting
        - Minimal manual state handling
        """
        supervisor_logger.log_structured(
            level="INFO",
            message=f"[stream] START",
            task_id=task_id,
            context_id=context_id,
            extra={
                "agent_name": self.__class__.__name__, 
                "query_or_command": str(query_or_command),
                "is_resume": isinstance(query_or_command, Command)
            }
        )
        # Simple init/resume handling
        if isinstance(query_or_command, Command):
            # Resume call: pass Command directly to graph
            # LangGraph will restore state from checkpoint using thread_id
            # The resume value becomes the return value of interrupt() inside nodes
            graph_input = query_or_command
            thread_id = context_id  # Reuse for HITL resume - must match original thread_id
            
            supervisor_logger.log_structured(
                level="INFO",
                message="Resuming graph execution from checkpoint",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "thread_id": thread_id,
                    "resume_value_type": type(query_or_command.resume).__name__,
                    "has_resume_value": query_or_command.resume is not None
                }
            )
        else:
            # Initial call: create full state with Pydantic models
            user_query = str(query_or_command)
            graph_input = self._create_initial_state(
                user_query=user_query,
                context_id=context_id,
                task_id=task_id
            )
            thread_id = context_id  # Use context_id as thread_id for checkpoint consistency
            
            supervisor_logger.log_structured(
                level="INFO",
                message="Created initial supervisor state",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "workflow_id": graph_input["workflow_state"].workflow_id,
                    "initial_phase": graph_input.get("active_phase", "planning"),
                    "user_requirements_set": graph_input.get("user_requirements") is not None
                }
            )

        config: RunnableConfig = {
            'configurable': {
                'thread_id': thread_id,
                # Increase recursion limit for complex multi-phase workflows
                # Config stores keys as lowercase attributes (from _set_attributes)
                'recursion_limit': getattr(self.config_instance, 'recursion_limit', 50)
            }
        }
        step_count = 0
        config_with_durability = {
            **config,
            "durability": "async",
            "subgraphs": True
        }
        try:
            # Use astream with values mode and subgraphs=True for proper handoff processing
            # This ensures proper subgraph handoff processing and prevents NoneType iteration errors
            async with isolation_shield():
                async for item in self.compiled_graph.astream(graph_input, config_with_durability, stream_mode='values', subgraphs=True):
                    step_count += 1
                    
                    # When subgraphs=True, items are tuples (namespace, state)
                    # Unpack to get the actual state dictionary
                    if isinstance(item, tuple) and len(item) == 2:
                        namespace, state = item
                        item = state  # Use the state dict for processing
                    
                    # 1. Handle human-in-the-loop interrupt (enhanced HITL support)
                    if '__interrupt__' in item:
                        interrupt_response = self._handle_hitl_interrupt(
                            item=item,
                            context_id=context_id,
                            task_id=task_id,
                            step_count=step_count
                        )
                        if interrupt_response:
                            yield interrupt_response
                            # Pause streaming until client resumes with feedback
                            # The graph execution is paused at the interrupt point
                            # No more items will be streamed until resume with Command(resume=...)
                            break                    
                    # 2. Check for workflow completion to prevent infinite loops
                    workflow_state = item.get('workflow_state', {})
                    human_approval_status = item.get('human_approval_status', {})
                    
                    # Check if workflow is complete (all phases done and deployment approved)
                    is_workflow_complete = False
                    planning_complete = False
                    generation_complete = False
                    validation_complete = False
                    deployment_approved = False
                    
                    if isinstance(workflow_state, dict):
                        planning_complete = workflow_state.get('planning_complete', False)
                        generation_complete = workflow_state.get('generation_complete', False)
                        validation_complete = workflow_state.get('validation_complete', False)
                        
                        if isinstance(human_approval_status, dict):
                            deployment_approval = human_approval_status.get('deployment')
                            if deployment_approval:
                                if isinstance(deployment_approval, dict):
                                    deployment_approved = deployment_approval.get('status') == 'approved'
                                elif hasattr(deployment_approval, 'status'):
                                    deployment_approved = deployment_approval.status == 'approved'
                        
                        is_workflow_complete = (
                            planning_complete and 
                            generation_complete and 
                            validation_complete and 
                            deployment_approved
                        )
                    elif hasattr(workflow_state, 'planning_complete'):
                        # Handle Pydantic object
                        planning_complete = workflow_state.planning_complete
                        generation_complete = workflow_state.generation_complete
                        validation_complete = workflow_state.validation_complete
                        deployment_approved = self._check_approval_status(item, "deployment")
                        
                        is_workflow_complete = (
                            planning_complete and
                            generation_complete and
                            validation_complete and
                            deployment_approved
                        )
                    
                    # If workflow is complete, stop execution
                    if is_workflow_complete:
                        supervisor_logger.log_structured(
                            level="INFO",
                            message="Workflow complete - stopping execution",
                            task_id=task_id,
                            context_id=context_id,
                            extra={
                                'step_count': step_count,
                                'planning_complete': planning_complete,
                                'generation_complete': generation_complete,
                                'validation_complete': validation_complete,
                                'deployment_approved': deployment_approved
                            }
                        )
                        yield AgentResponse(
                            response_type='text',
                            is_task_complete=True,
                            require_user_input=False,
                            content='✅ Workflow complete: All phases finished and deployment approved.',
                            metadata={
                                'session_id': context_id,
                                'task_id': task_id,
                                'agent_name': self.name,
                                'step_count': step_count,
                                'status': 'workflow_complete'
                            }
                        )
                        break  # Stop streaming
                    
                    # 3. Check recursion limit warning
                    recursion_limit = config_with_durability.get('configurable', {}).get('recursion_limit', 50)
                    if step_count >= (recursion_limit - 10):  # Warn 10 steps before hitting limit
                        supervisor_logger.log_structured(
                            level="WARNING",
                            message="Approaching recursion limit",
                            task_id=task_id,
                            context_id=context_id,
                            extra={
                                'step_count': step_count,
                                'recursion_limit': recursion_limit,
                                'remaining_steps': recursion_limit - step_count
                            }
                        )
                    
                    # 4. Handle normal state updates (direct status-based responses like reference)
                    status = item.get('status')
                    if status == 'completed':
                        # Get planning data and status for individual agent completion
                        is_complete, planning_status = self._validate_planner_agent_completion(item)
                            
                        if is_complete:
                            # Extract the actual planning data
                            planning_data = planning_status.get('data_values', {})
                            completion_metrics = planning_status.get('completion_metrics', {})
                                
                             # Create comprehensive content with real planning data
                            content_data = {
                                'status': 'planning_complete',
                                'completion_metrics': completion_metrics,
                                'requirements_data': planning_data.get('requirements_data', ''),
                                'execution_data': planning_data.get('execution_data', ''),
                                'planning_state': planning_status.get('planning_state', {}),
                                'validation_result': planning_status.get('validation_result', 'PASS')
                            }
                                
                            yield AgentResponse(
                                response_type='text',
                                is_task_complete=True,
                                require_user_input=False,
                                content=str(content_data),
                                metadata={
                                    'session_id': context_id,
                                    'task_id': task_id,
                                    'agent_name': self.name,
                                    'step_count': step_count,
                                    'status': 'planning_data_available',
                                    'planning_complete': True
                                }
                            )
                            continue  # Continue to allow handoff tool execution
                        else:
                            # Individual agent completed, but supervisor workflow not fully complete
                            yield AgentResponse(
                                response_type='text',
                                is_task_complete=False,
                                require_user_input=False,
                                content=f'Agent completed - continuing supervisor workflow...',
                                metadata={
                                    'session_id': context_id,
                                    'task_id': task_id,
                                    'agent_name': self.name,
                                    'step_count': step_count,
                                    'status': 'agent_completed_workflow_continuing'
                                }
                            )
                    else:
                        # Default processing response
                        yield AgentResponse(
                            response_type='text',
                            is_task_complete=False,
                            require_user_input=False,
                            content='Processing...',
                            metadata={
                                'session_id': context_id,
                                'task_id': task_id,
                                'agent_name': self.name,
                                'step_count': step_count,
                                'status': 'working'
                            }
                        )
        except Exception as e:
            supervisor_logger.log_structured(
                level="ERROR",
                message=f"Stream execution failed: {e}",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "thread_id": thread_id,
                    "step_count": step_count
                }
            )
            yield AgentResponse(
                response_type='error',
                is_task_complete=True,
                require_user_input=False,
                content=f'Error during streaming: {str(e)}',
                error=str(e),
                metadata={
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'error'
                }
            )
        
            supervisor_logger.log_structured(
                level="INFO",
                message=f"[stream] END",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "agent_name": self.__class__.__name__,
                    "thread_id": thread_id,
                    "step_count": step_count
                }
            )

    @log_sync
    def is_ready(self) -> bool:
        """Check if the supervisor is ready for use."""
        model_ready = self.model is not None
        supervisor_ready = self.compiled_graph is not None
        agents_ready = len(self.agents) > 0
        
        # Log detailed status for debugging
        if not model_ready:
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Supervisor not ready: LLM model not initialized",
                extra={"model_ready": model_ready, "supervisor_ready": supervisor_ready, "agents_ready": agents_ready}
            )
        elif not supervisor_ready:
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Supervisor not ready: Supervisor graph not compiled",
                extra={"model_ready": model_ready, "supervisor_ready": supervisor_ready, "agents_ready": agents_ready}
            )
        elif not agents_ready:
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Supervisor not ready: No agent subgraphs registered",
                extra={"model_ready": model_ready, "supervisor_ready": supervisor_ready, "agents_ready": agents_ready}
            )
        
        # Return true only if all components are ready
        return model_ready and supervisor_ready and agents_ready


    @log_sync
    def list_agents(self) -> List[str]:
        """List all available agents."""
        agent_list = list(self.agents.keys())
        supervisor_logger.log_structured(
            level="DEBUG",
            message="Listed available agents",
            extra={"agent_count": len(agent_list), "agents": agent_list}
        )
        return agent_list
        
# Factory function for easy supervisor creation
def create_k8sAutopilotSupervisorAgent(
    agents: List[BaseSubgraphAgent],
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    prompt_template: Optional[str] = None,
    name: str = "k8sAutopilotSupervisorAgent"
) -> k8sAutopilotSupervisorAgent:
    """
    Create a k8sAutopilotSupervisorAgent with the given agents using centralized configuration.
    
    Args:
        agents: List of subgraph agents to orchestrate
        config: Configuration instance (defaults to new Config())
        custom_config: Optional custom configuration to override defaults
        prompt_template: Custom prompt template
        name: Name of the k8sAutopilotSupervisorAgent
        
    Returns:
        Configured k8sAutopilotSupervisorAgent
    """
    return k8sAutopilotSupervisorAgent(
        agents=agents,
        config=config,
        custom_config=custom_config,
        prompt_template=prompt_template,
        name=name
    )