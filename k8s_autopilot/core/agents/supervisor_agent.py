import uuid
import ast
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable, AsyncGenerator, Annotated, Tuple, Union
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Send, Command, interrupt
from langgraph.checkpoint.memory import MemorySaver
import asyncio
from contextlib import asynccontextmanager, aclosing
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
from langchain.chat_models import init_chat_model

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

        # Initialize the LLM model using init_chat_model
        try:
            # Remove 'provider' key as it's handled by model_provider or auto-inference
            config_for_init = {k: v for k, v in llm_config.items() if k != 'provider'}
            self.model = init_chat_model(**config_for_init)
            supervisor_logger.log_structured(
                level="INFO",
                message=f"Initialized LLM model: {llm_config.get('provider', 'auto')}:{llm_config.get('model', 'unknown')}",
                extra={"llm_provider": llm_config.get('provider', 'auto'), "llm_model": llm_config.get('model', 'unknown')}
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
You are a supervisor managing specialized swarms for Kubernetes Helm chart generation, management, and deployment.

**Capabilities:**
1. **Generation:** Create new Helm charts, templates, and values files from scratch.
2. **Management:** Install, upgrade, list, delete, and troubleshoot existing Helm releases on the cluster.
3. **Cluster/Context Management:** List Kubernetes contexts, switch between clusters, and query Helm releases across different clusters.

**VALID REQUEST EXAMPLES:**
- "Create a Helm chart for nginx" (Generation)
- "Install the nginx chart into the default namespace" (Management)
- "Upgrade my-release to version 2.0" (Management)
- "List all installed helm charts" (Management)
- "Troubleshoot why my release is failing" (Management)
- "List all Kubernetes contexts" (Management - Cluster/Context)
- "Switch to production cluster" (Management - Cluster/Context)
- "Show Helm releases in the production cluster" (Management - Cluster/Context)
- "What clusters do I have access to?" (Management - Cluster/Context)

**OUT-OF-SCOPE REQUEST HANDLING:**
If a request is NOT related to Helm usage (e.g. "Write a Python script", "Configure AWS VPC", greetings like "hello", "how are you"):
1. **CRITICAL: You MUST use the request_human_feedback tool** - DO NOT output text directly
2. **Create dynamic, contextual messages** based on the user's input:
   - For greetings (hello, hi, how are you): Greet them naturally first, then explain your capabilities
   - For out-of-scope requests: Acknowledge their request, then guide them to Helm-related tasks
   - Example for "hello how are you": "Hello! I'm doing well, thank you for asking. I'm specialized in Kubernetes Helm charts - I can help you generate new charts or manage existing ones. What would you like to work on today?"
   - Example for "what is Jenkins": "I can help with Kubernetes Helm charts, but Jenkins configuration is outside my scope. I specialize in Helm chart generation and management. Would you like help creating a Helm chart for deploying Jenkins instead?"
3. **NEVER output conversational text without calling request_human_feedback tool first**
4. **Make your messages natural and conversational** - adapt to the user's tone and context

**FEEDBACK TOOL USAGE:**
- request_human_feedback: Use this tool when you need to:
  * **MANDATORY for greetings** (hello, hi, how are you, etc.) - ALWAYS call this tool with a friendly, contextual greeting
  * **MANDATORY for out-of-scope requests** - ALWAYS call this tool with a helpful, contextual response that guides the user
  * Clarify ambiguous requirements
  * Get approval for decisions
  * Request human input to proceed
  * **Final Review**: Notify user of generated artifacts/successful operations
  * **Status Updates**: Tell the user what you are doing if a step is taking time

Available tools:
- transfer_to_planning_swarm: Analyze requirements and create Helm chart architecture plans (Generation)
- transfer_to_template_supervisor: Generate Helm chart templates and values files (Generation)
- transfer_to_validator_deep_agent: Validate charts, perform security scanning, and prepare deployment configs (Generation)
- transfer_to_helm_management: Install, upgrade, list, delete, or troubleshoot Helm charts (Management). Also handles Kubernetes context management (list contexts, switch contexts, query across clusters).
- request_human_feedback: Request human feedback or clarification

**HITL APPROVAL GATES (REQUIRED):**
- request_generation_review: Request human review of generated artifacts and workspace selection (call after template_supervisor completes)

**Workflow Logic:**

1. **For Generation Requests** ("Create a chart..."):
   - transfer_to_planning_swarm -> template_supervisor -> **request_generation_review** -> validator_deep_agent
   
2. **For Management/Operation Requests** ("Install...", "List...", "Fix...", "Show contexts...", "Switch to cluster..."):
   - DIRECTLY call `transfer_to_helm_management(task_description=user_request)`
   - This includes:
     * Helm release operations (install, upgrade, list, delete, troubleshoot)
     * Kubernetes context operations (list contexts, switch contexts, query across clusters)
     * Chart queries and information requests
   - Do NOT call planning or validation swarms unless the user asks to *modify* the chart code first.

**WORKFLOW SEQUENCE WITH HITL (For Generation Requests):**
1. For ANY Helm chart generation request → transfer_to_planning_swarm(task_description="...")
2. When planning_complete → transfer_to_template_supervisor(task_description="...") [Proceeds automatically]
3. **CRITICAL**: When generation_complete (from template_supervisor) → **STOP and call request_generation_review() IMMEDIATELY**. [REQUIRED BLOCKING STEP]
   - Do NOT proceed to validation.
   - Do NOT ask for feedback yet.
   - Do NOT call any other tool.
   - **JUST call request_generation_review()** - this is MANDATORY.
4. When generation_approved → transfer_to_validator_deep_agent(task_description="...")
5. When validation_complete → **Workflow Complete**:
   - The system will automatically display the completion message when validation succeeds (all checks pass)
   - If validation fails, the user will see error details and can request fixes
   - No further action needed - the workflow is complete after successful validation

**CRITICAL RULES:**
- Always check if the user wants to GENERATE a new chart or MANAGE an existing one.
- For Management tasks (Helm operations, context management, cluster queries), delegate to `transfer_to_helm_management` immediately.
- Kubernetes context queries (list contexts, switch contexts, query releases in specific clusters) are Management operations - route to `transfer_to_helm_management`.
- Do NOT try to run helm commands yourself. Use the tools.
- **Check workflow_state flags before each tool call**
- **ALWAYS call HITL gate tools after phase completion (generation → request_generation_review)**
- **Do NOT proceed to next phase without approval (check human_approval_status)**
- If approval status is "pending" or "rejected", wait or end workflow
- Always call tools immediately, don't describe what you will do
- Do NOT do any chart generation/validation yourself - ONLY delegate using tools
- Template generation proceeds automatically after planning completes (no approval needed)
- Validation proceeds automatically ONLY after generation is approved
- **NO AUTOMATED DEPLOYMENT**: Use request_human_feedback with `mark_deployment_complete=True` for final step.

**HITL GATE RULES:**
- **request_generation_review**: Call IMMEDIATELY after template_supervisor completes. Do NOT proceed to validation without approval. If generation_approved is False, you MUST call this tool. You cannot skip it.
- Planning phase does NOT require approval - proceed directly to template generation

**STOP CONDITIONS (When to finish):**
- When workflow_state.workflow_complete == True → Respond with completion summary and end.
- When any phase is rejected AND user doesn't request changes → End workflow with error message.
- DO NOT keep calling tools if workflow is already complete - check workflow_state first.

**IMPORTANT**: For requests like "help me write nginx helm chart", immediately call:
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
                    message="Planning swarm tool invoked - using astream(subgraphs=True)",
                    extra={"task_description": task_description, "tool_call_id": tool_call_id}
                )
                
                # 1. Transform supervisor state → planning state
                planning_input = StateTransformer.supervisor_to_planning(runtime.state)
                
                # 2. Configure Stateful Execution for Resume Capability
                session_id = runtime.state.get("session_id", "default_session")
                child_config = {
                    # "configurable": {"thread_id": f"{session_id}_planning"},
                    "recursion_limit": 100
                }
                
                # Check if we are resuming an interrupted state
                # target_input = planning_input
                # try:
                #     current_snapshot = planning_swarm.get_state(child_config)
                #     if current_snapshot.next:
                #         supervisor_logger.log_structured(
                #             level="INFO",
                #             message="Resuming planning swarm from interrupt",
                #             extra={"next_nodes": current_snapshot.next}
                #         )
                #         messages = runtime.state.get("messages", [])
                #         if messages:
                #             last_msg = messages[-1]
                #             if hasattr(last_msg, 'content'):
                #                 target_input = Command(resume=last_msg.content)
                # except Exception:
                #     pass

                # 3. Use ainvoke (Stateful)
                # Parent checks events via subgraphs=True, so simple ainvoke is sufficient
                final_state = await planning_swarm.ainvoke(
                    planning_input,
                    config=child_config
                )
                
                if final_state is None:
                    raise ValueError("Planning swarm execution yielded no state")

                # 3. Transform back (note: state updates happen automatically via tool return)
                supervisor_updates = StateTransformer.planning_to_supervisor(
                    final_state,
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
                    message="Generation swarm tool invoked - using astream(subgraphs=True)",
                    extra={"task_description": task_description, "tool_call_id": tool_call_id}
                )
                
                # 1. Transform supervisor state → generation state
                generation_input = StateTransformer.supervisor_to_generation(runtime.state)
                
                # 2. Configure Stateful Execution for Resume Capability
                session_id = runtime.state.get("session_id", "default_session")
                child_config = {
                    # "configurable": {"thread_id": f"{session_id}_generation"},
                    "recursion_limit": 100
                }
                
                # Check for resume
                # target_input = generation_input
                # try:
                #     current_snapshot = template_supervisor.get_state(child_config)
                #     if current_snapshot.next:
                #         supervisor_logger.log_structured(
                #             level="INFO",
                #             message="Resuming generation swarm from interrupt",
                #             extra={"next_nodes": current_snapshot.next}
                #         )
                #         messages = runtime.state.get("messages", [])
                #         if messages:
                #             last_msg = messages[-1]
                #             if hasattr(last_msg, 'content'):
                #                 target_input = Command(resume=last_msg.content)
                # except Exception:
                #     pass

                # 3. Use ainvoke (Stateful)
                final_state = await template_supervisor.ainvoke(
                    generation_input,
                    config=child_config
                )
                
                if final_state is None:
                    raise ValueError("Generation swarm execution yielded no state")

                # 3. Transform back (note: state updates happen automatically via tool return)
                supervisor_updates = StateTransformer.generation_to_supervisor(
                    final_state,
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
                runtime: ToolRuntime[None, MainSupervisorState],
                tool_call_id: Annotated[str, InjectedToolCallId],
                workspace_dir: Optional[str] = None
            ) -> Command:
                """
                Delegate to validation swarm for security and quality validation.
                
                Use this when:
                - Generation is complete (workflow_state.generation_complete == True)
                - Security approval granted (workflow_state.security_approved == True)
                - Need to validate generated Helm charts
                - active_phase == "generation"
                
                Args:
                    task_description: Description of the validation task
                    workspace_dir: Optional. Explicitly specify the directory where the chart is located. 
                                   If not provided, defaults to the 'workspace_dir' in the state.
                                   The supervisor should pass this if the user specified a location.
                """
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Validation swarm tool invoked - using astream(subgraphs=True)",
                    extra={"task_description": task_description, "workspace_dir": workspace_dir}
                )
                
                # 1. Transform supervisor state → validation state
                # Priority: Argument > State > Default
                final_workspace_dir = workspace_dir or runtime.state.get("workspace_dir", "/tmp/helm-charts")
                
                # If explicitly provided by supervisor, ensure state is updated
                state_updates = {}
                if workspace_dir and workspace_dir != runtime.state.get("workspace_dir"):
                    state_updates["workspace_dir"] = workspace_dir
                
                validation_input = StateTransformer.supervisor_to_validation(
                    runtime.state, 
                    workspace_dir=final_workspace_dir
                )
                
                # 2. Use astream(subgraphs=True) for proper HITL interrupt propagation
                final_state = None
                # session_id = runtime.state.get("session_id", "default_session")
                child_config = {
                    # "configurable": {"thread_id": f"{session_id}_validation"},
                    "recursion_limit": 100
                }
                
                
                final_state = await validator_deep_agent.ainvoke(
                    validation_input,
                    config=child_config
                )
                
                if final_state is None:
                    raise ValueError("Validation swarm execution yielded no state")

                # 3. Transform back
                supervisor_updates = StateTransformer.validation_to_supervisor(
                    final_state,
                    runtime.state,
                    tool_call_id=tool_call_id
                )
                
                # Merge our workspace update if needed
                if state_updates:
                    supervisor_updates.update(state_updates)
                
                # Return Command with state updates
                return Command(
                    update=supervisor_updates
                )
            
            tools.append(transfer_to_validator_deep_agent)
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Created validation swarm tool",
                extra={"tool_name": "transfer_to_validator_deep_agent"}
            )
        
        # Helm Management agent tool
        if "helm_mgmt_deep_agent" in compiled_swarms:
            helm_mgmt_agent = compiled_swarms["helm_mgmt_deep_agent"]
            
            @tool
            async def transfer_to_helm_management(
                task_description: str,
                runtime: ToolRuntime[None, MainSupervisorState],
                tool_call_id: Annotated[str, InjectedToolCallId]
            ) -> Command:
                """
                Delegate to Helm Management Agent for existing chart operations.
                
                Use this when:
                - User requests to INSTALL, UPGRADE, LIST, or DELETE Helm charts
                - User asks for troubleshooting or status of installed releases
                - User asks to search for charts (without generating new code)
                
                Args:
                    task_description: Description of the management task (e.g., "Install nginx chart")
                """
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Helm Management agent tool invoked - using astream(subgraphs=True)",
                    extra={"task_description": task_description}
                )
                
                # 1. Transform State
                mgmt_input = StateTransformer.supervisor_to_helm_mgmt(runtime.state)
                # Overwrite user_request with task_description if provided, as it's more specific
                if task_description:
                   mgmt_input["user_request"] = task_description
                   # Also update message content to reflect specific task
                   mgmt_input["messages"] = [HumanMessage(content=task_description)]

                # 2. Use astream(subgraphs=True) for proper HITL interrupt propagation
                final_state = None
                # session_id = runtime.state.get("session_id", "default_session")
                child_config = {
                    # "configurable": {"thread_id": f"{session_id}_helm_mgmt"},
                    "recursion_limit": 50
                }
                final_state = await helm_mgmt_agent.ainvoke(
                    mgmt_input,
                    config=child_config
                )
                
                if final_state is None:
                    raise ValueError("Helm management agent execution yielded no state")

                # 3. Transform Back
                supervisor_updates = StateTransformer.helm_mgmt_to_supervisor(
                    final_state,
                    runtime.state,
                    tool_call_id
                )
                
                return Command(update=supervisor_updates)
            
            tools.append(transfer_to_helm_management)
            supervisor_logger.log_structured(
                level="DEBUG",
                message="Created helm management agent tool",
                extra={"tool_name": "transfer_to_helm_management"}
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
        
        if phase == "generation":
            # Should call if generation is complete but not approved
            generation_complete = workflow_state.generation_complete if hasattr(workflow_state, "generation_complete") else workflow_state.get("generation_complete", False)
            helm_chart_artifacts = state.get("helm_chart_artifacts", {})
            return generation_complete and len(helm_chart_artifacts) > 0
        
        return False

    @tool
    def request_human_feedback(
        question: str,
        context: Optional[str] = None,
        phase: Optional[str] = None,
        mark_deployment_complete: bool = False,
        runtime: ToolRuntime[None, MainSupervisorState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = ""
    ) -> Command:
        """
        Request human feedback during workflow execution.
        
        This tool pauses execution and waits for human input. Use this when:
        - You need clarification on ambiguous requirements
        - You need approval for a decision
        - You need human input to proceed
        - **For greetings**: Create a friendly, contextual greeting that acknowledges the user's message
        - **For out-of-scope requests**: Create a helpful response that guides the user to Helm-related tasks
        
        **IMPORTANT for greetings and out-of-scope requests:**
        - Create dynamic, contextual messages based on what the user said
        - For greetings: Greet naturally first, then explain your capabilities
        - For out-of-scope: Acknowledge their request, then guide them appropriately
        - Make messages natural and conversational - adapt to the user's tone
        - DO NOT use static messages - personalize based on the user's input
        
        Examples:
        - User: "hello how are you" → question="Hello! I'm doing well, thank you. I specialize in Kubernetes Helm charts - I can help you generate new charts or manage existing ones. What would you like to work on?"
        - User: "what is Jenkins" → question="I can help with Kubernetes Helm charts, but Jenkins configuration is outside my scope. I specialize in Helm chart generation and management. Would you like help creating a Helm chart for deploying Jenkins instead?"
        
        Args:
            question: The question or request for the human (should be dynamic and contextual, not static)
            context: Optional context about why feedback is needed
            phase: Optional current workflow phase
            mark_deployment_complete: If True, marks deployment phase as complete/approved when user replies
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
                "session_id": session_id,
                "mark_deployment_complete": mark_deployment_complete
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
        tool_message_content = f"User has replied: {human_response_str}"
        tool_message = ToolMessage(content=tool_message_content, tool_call_id=tool_call_id)
        
        # Create human message for LLM input (separate from tool response)
        human_message = HumanMessage(content=human_response_str)
        
        # Determine state updates
        update_dict = {
            "user_query": human_response_str,
            "messages": [tool_message],  # Only tool message to respond to tool call
            "llm_input_messages": [human_message],  # Human message for LLM input
        }
        
        # Handle marking deployment as complete if requested
        if mark_deployment_complete and runtime:
            # GUARDRAIL: Verify validation is actually complete
            # Prevents agent from hallucinating validation or skipping the validation phase
            current_ws = runtime.state.get("workflow_state")
            is_val_complete = False
            if isinstance(current_ws, dict):
                is_val_complete = current_ws.get("validation_complete", False)
            elif current_ws:
                is_val_complete = getattr(current_ws, "validation_complete", False)
                
            if not is_val_complete:
                supervisor_logger.log_structured(
                    level="WARNING",
                    message="Agent attempted to complete deployment before validation",
                    extra={"task_id": runtime.state.get("task_id")}
                )
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content="SYSTEM ERROR: You cannot mark deployment complete because the 'validation' phase is not complete. You skipped the validation step. \n\nREQUIRED ACTION:\n1. Call 'request_generation_review' NOW.\n2. Then call 'transfer_to_validator_deep_agent'.",
                                tool_call_id=tool_call_id
                            )
                        ]
                    }
                )

            try:
                # Retrieve current workflow state
                current_workflow_state = runtime.state.get("workflow_state")
                
                # Check if it's a dict or object and convert/copy if needed
                if isinstance(current_workflow_state, dict):
                    workflow_state_obj = SupervisorWorkflowState(**current_workflow_state)
                elif current_workflow_state:
                    # It's an object, we can modify it directly or copy it
                    # Pydantic models are mutable by default
                    workflow_state_obj = current_workflow_state
                else:
                    # Create new if missing
                    workflow_state_obj = SupervisorWorkflowState()
                
                # Update the flags
                workflow_state_obj.set_phase_complete("deployment")
                workflow_state_obj.set_approval("deployment", True)
                
                # Add to updates
                update_dict["workflow_state"] = workflow_state_obj
                
                # Add a system note to messages to confirm
                confirmation_msg = ToolMessage(
                    content="Deployment phase marked as complete based on user interaction.",
                    tool_call_id=tool_call_id # Reusing same ID might be confusing but necessary for tool output
                )
                # Actually we can't add two tool messages for same ID easily, just append to content above
                tool_message.content += " (Deployment phase marked complete)"
                
            except Exception as e:
                supervisor_logger.log_structured(
                    level="ERROR",
                    message="Failed to update workflow state in request_human_feedback",
                    extra={"error": str(e)}
                )

        # Return Command to update state with human response
        return Command(
            update=update_dict,
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
                # Schema: {"pending_tool_calls": {"tool_call_id": {"tool_name": ..., "is_critical": ...}}}
                raw_tool_call_data = interrupt_payload.get("pending_tool_calls", {})
                
                # Default values
                tool_call_id = "unknown"
                tool_call_item = {}
                
                # Extract the first tool call (current implementation assumes single interrupt)
                if raw_tool_call_data and isinstance(raw_tool_call_data, dict):
                    # Get the first key/value pair
                    try:
                        tool_call_id, tool_call_item = next(iter(raw_tool_call_data.items()))
                    except StopIteration:
                        pass
                
                # Extract tool call information from the nested item
                tool_name = tool_call_item.get("tool_name", "unknown")
                tool_args = tool_call_item.get("tool_args", {})
                is_critical = tool_call_item.get("is_critical", False)
                phase = tool_call_item.get("phase", "unknown")
                reason = tool_call_item.get("reason", "Approval required for critical operation")
                status = tool_call_item.get("status", "pending")
                
                content = {
                    'type': 'tool_call_approval_request',
                    'tool_call_id': tool_call_id,
                    'tool_name': tool_name,
                    'tool_args': tool_args,
                    'is_critical': is_critical,
                    'phase': phase,
                    'reason': reason,
                    'status': status,
                    'session_id': tool_call_item.get('session_id', context_id)
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
                    'interrupt_data': raw_tool_call_data
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
                
            elif "phase" in interrupt_payload and "summary" in interrupt_payload:
                # Handle HITL gate interrupts (planning_review_gate, generation_review_gate, etc.)
                # These interrupts have phase, summary, and other metadata fields
                phase = interrupt_payload.get('phase', 'unknown')
                summary = interrupt_payload.get('summary', 'Human review required')
                required_action = interrupt_payload.get('required_action', 'approve')
                options = interrupt_payload.get('options', ['approve', 'reject'])
                
                content = {
                    'type': 'hitl_gate_interrupt',
                    'phase': phase,
                    'summary': summary,  # Use the rich markdown summary
                    'message': summary,  # Also include as message for backward compatibility
                    'required_action': required_action,
                    'options': options,
                    'chart_name': interrupt_payload.get('chart_name'),
                    'chart_files': interrupt_payload.get('chart_files', []),
                    'file_count': interrupt_payload.get('file_count', 0),
                    'workspace_dir_prompt': interrupt_payload.get('workspace_dir_prompt', False),
                    'review_type': interrupt_payload.get('review_type', 'generic'),
                    'data': interrupt_payload
                }
                
                interrupt_metadata = {
                    'session_id': context_id,
                    'task_id': task_id,
                    'agent_name': self.name,
                    'step_count': step_count,
                    'status': 'hitl_gate_interrupt',
                    'interrupt_type': 'hitl_gate',
                    'phase': phase,
                    'required_action': required_action,
                    'interrupt_data': interrupt_payload
                }
                
                supervisor_logger.log_structured(
                    level="INFO",
                    message=f"HITL gate interrupt detected: {phase}",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        'phase': phase,
                        'required_action': required_action,
                        'has_summary': bool(summary),
                        'summary_preview': summary[:100] if summary else None
                    }
                )
                
            else:
                # Generic/unknown interrupt type
                # Check for summary field as fallback (some interrupts may have summary instead of message)
                message = interrupt_payload.get('message') or interrupt_payload.get('summary', 'Human input required')
                
                content = {
                    'type': 'generic_interrupt',
                    'message': message,
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
        
        # Create HITL gate tools
        hitl_gate_tools = self._create_hitl_gate_tools()
        
        # Create human feedback tool
        feedback_tool = self.request_human_feedback
        
        # Combine all tools
        all_tools = swarm_tools + hitl_gate_tools + [feedback_tool]
        
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

    def _handle_direct_model_text(
        self,
        messages: List[Any],
        task_id: str,
        context_id: str,
        step_count: int
    ) -> Optional[AgentResponse]:
        """
        Handle cases where the model generates direct text without using a tool.
        This provides a fallback for "chatty" model behavior while strict tool usage
        is preferred.
        
        Args:
            messages: List of messages from the graph state
            task_id: Current task identifier
            context_id: Current context/session identifier
            step_count: Current step count in execution
            
        Returns:
            AgentResponse if a direct text message is found and needs yielding, None otherwise.
        """
        if not messages:
            return None
            
        last_message = messages[-1]
        
        # Check if it's an AI message with content but NO tool calls
        if isinstance(last_message, AIMessage):
            has_tool_calls = bool(getattr(last_message, 'tool_calls', None))
            content_str = str(last_message.content) if last_message.content else ""
            has_content = bool(content_str and content_str.strip())
            
            if has_content and not has_tool_calls:
                supervisor_logger.log_structured(
                    level="WARNING",
                    message="Model output text without tool calls - should have used request_human_feedback",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        "content_preview": content_str[:100],
                        "note": "This flows through but model should use request_human_feedback tool"
                    }
                )
                
                return AgentResponse(
                    response_type='text',
                    is_task_complete=False,
                    require_user_input=False,  # Don't require input - let graph complete/loop
                    content=content_str,
                    metadata={
                        'session_id': context_id,
                        'task_id': task_id,
                        'agent_name': self.name,
                        'step_count': step_count,
                        'status': 'working',
                        'warning': 'Text response without tool call - should use request_human_feedback'
                    }
                )
        
        return None

    def _check_helm_mgmt_completion(
        self,
        item: Dict[str, Any],
        task_id: str,
        context_id: str,
        step_count: int
    ) -> Optional[AgentResponse]:
        """
        Check if the Helm Management Agent workflow has completed and return the response if so.
        
        Args:
            item: Current state item from the graph stream
            task_id: Current task identifier
            context_id: Current context/session identifier
            step_count: Current step count in execution
            
        Returns:
            AgentResponse if the Helm Management workflow is complete, None otherwise.
        """
        # Extract workflow state and status
        workflow_state = item.get('workflow_state', {})
        status = item.get('status', 'pending')
        active_phase = item.get('active_phase', '')
        
        # Extract helm_mgmt_complete flag from workflow_state
        helm_mgmt_complete = False
        if isinstance(workflow_state, dict):
            helm_mgmt_complete = workflow_state.get('helm_mgmt_complete', False)
        elif hasattr(workflow_state, 'helm_mgmt_complete'):
            # Handle Pydantic object
            helm_mgmt_complete = workflow_state.helm_mgmt_complete
            
        if not helm_mgmt_complete:
            return None
            
        # Extract the helm_mgmt response (set by state_transformer.helm_mgmt_to_supervisor)
        helm_mgmt_response = item.get('helm_mgmt_response', '')
        
        if not helm_mgmt_response:
            supervisor_logger.log_structured(
                level="WARNING",
                message="Helm Management Agent completed but no response found",
                task_id=task_id,
                context_id=context_id
            )
            helm_mgmt_response = 'Helm Management Agent completed successfully.'
        
        supervisor_logger.log_structured(
            level="INFO",
            message="Helm Management Agent completed - yielding response",
            task_id=task_id,
            context_id=context_id,
            extra={
                "status": status,
                "active_phase": active_phase,
                "helm_mgmt_complete": helm_mgmt_complete
            }
        )
        
        # Return the response to be yielded
        return AgentResponse(
            response_type='data',
            is_task_complete=True,  # Helm mgmt operations are complete when agent finishes
            require_user_input=False,
            content={
                "status": "completed",
                "message": helm_mgmt_response,
                "helm_mgmt_response": helm_mgmt_response,
                "completion_metrics": {
                    'step_count': step_count,
                    'helm_mgmt_complete': True
                }
            },
            metadata={
                'session_id': context_id,
                'task_id': task_id,
                'agent_name': self.name,
                'step_count': step_count,
                'status': 'completed',
                'helm_mgmt_complete': True,
                'active_phase': active_phase or 'helm_mgmt_swarm'
            }
        )

    def _check_helm_generation_completion(
        self,
        item: Dict[str, Any],
        task_id: str,
        context_id: str,
        step_count: int
    ) -> Union[Optional[AgentResponse], str]:
        """
        Check if the Helm Generation workflow (planning/generation/validation) has completed.
        Also handles intermediate phase completions (e.g., validation completion).
        
        Args:
            item: Current state item from the graph stream
            task_id: Current task identifier
            context_id: Current context/session identifier
            step_count: Current step count in execution
            
        Returns:
            - AgentResponse: If workflow is complete or intermediate phase (validation) is complete
            - "CONTINUE_LOOP": If workflow is complete but we need to wait for agent message (continue loop)
            - None: If workflow is not complete
        """
        workflow_state = item.get('workflow_state', {})
        human_approval_status = item.get('human_approval_status', {})
        validation_results = item.get('validation_results', [])
        
        # Extract phase completion flags for helm generation workflow
        planning_complete = False
        generation_complete = False
        validation_complete = False
        deployment_approved = False
        
        # Consolidate state extraction logic
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
        elif hasattr(workflow_state, 'planning_complete'):
            # Handle Pydantic object
            planning_complete = workflow_state.planning_complete
            generation_complete = workflow_state.generation_complete
            validation_complete = workflow_state.validation_complete
            deployment_approved = self._check_approval_status(item, "deployment")
        
        # Check if validation just completed (but workflow not fully complete)
        # This handles intermediate validation completion message
        if validation_complete and validation_results:
            messages = item.get("messages", [])
            last_message = messages[-1] if messages else None
            
            # Check if we've already shown validation completion
            if isinstance(last_message, AIMessage) and "Validation Complete!" in str(last_message.content):
                # Already shown, skip
                pass
            else:
                # Check if last message indicates validation just completed
                message_indicates_completion = False
                if last_message:
                    message_content = str(last_message.content) if last_message.content else ""
                    validation_completion_indicators = [
                        "Validation swarm finished",
                        "Validation phase finished",
                        "validation completed",
                        "validation checks passed"
                    ]
                    message_indicates_completion = any(
                        indicator.lower() in message_content.lower() 
                        for indicator in validation_completion_indicators
                    )
                
                # Show validation completion message if validation is complete
                if message_indicates_completion or (validation_results and len(validation_results) > 0):
                    # Count validation results
                    passed_count = 0
                    failed_count = 0
                    for r in validation_results:
                        is_passed = False
                        if hasattr(r, 'passed'):
                            is_passed = r.passed
                        elif isinstance(r, dict):
                            is_passed = r.get("passed", False)
                        
                        if is_passed:
                            passed_count += 1
                        else:
                            failed_count += 1
                    
                    total_checks = len(validation_results)
                    
                    # Build validation completion message
                    if failed_count == 0:
                        completion_message = (
                            f"✅ **Validation Complete!**\n\n"
                            f"All {total_checks} validation checks passed successfully:\n"
                        )
                        
                        # List validation checks that passed
                        for r in validation_results:
                            validator_name = ""
                            if hasattr(r, 'validator'):
                                validator_name = r.validator
                            elif isinstance(r, dict):
                                validator_name = r.get("validator", "Unknown")
                            
                            if validator_name:
                                completion_message += f"  ✓ {validator_name.replace('_', ' ').title()}\n"
                        
                        completion_message += (
                            f"\n📦 **Chart Status:** Ready for deployment\n"
                            f"📊 **Results:** {passed_count} passed, {failed_count} failed\n"
                            f"\n✅ **Workflow Complete!**\n\n"
                            f"Chart has been generated and validated. Please follow along the readme for deployment instructions.\n\n"
                            f"If you found this helpful, please support us by starring our repository: https://github.com/talkops-ai/k8s-autopilot 🌟"
                        )
                    else:
                        completion_message = (
                            f"⚠️ **Validation Complete with Issues**\n\n"
                            f"Validation finished with {failed_count} failed check(s) out of {total_checks} total:\n"
                        )
                        
                        # List validation results
                        for r in validation_results:
                            validator_name = ""
                            is_passed = False
                            if hasattr(r, 'validator'):
                                validator_name = r.validator
                                is_passed = r.passed
                            elif isinstance(r, dict):
                                validator_name = r.get("validator", "Unknown")
                                is_passed = r.get("passed", False)
                            
                            status_icon = "✓" if is_passed else "✗"
                            completion_message += f"  {status_icon} {validator_name.replace('_', ' ').title()}\n"
                        
                        completion_message += (
                            f"\n📦 **Chart Status:** Review required\n"
                            f"📊 **Results:** {passed_count} passed, {failed_count} failed\n"
                            f"\nPlease review the validation results and fix any issues before deployment."
                        )
                    
                    supervisor_logger.log_structured(
                        level="INFO",
                        message="Validation phase completed - generating completion message",
                        task_id=task_id,
                        context_id=context_id,
                        extra={
                            'validation_complete': validation_complete,
                            'passed_count': passed_count,
                            'failed_count': failed_count,
                            'total_checks': total_checks
                        }
                    )
                    
                    # Return validation completion message
                    # Always mark as complete when validation finishes, even if there are failures.
                    # The user can review the results and request fixes in a new turn if needed.
                    is_complete = True
                    
                    return AgentResponse(
                        response_type='text',
                        is_task_complete=is_complete,  # Complete if all validations passed
                        require_user_input=False,
                        content=completion_message,
                        metadata={
                            'session_id': context_id,
                            'task_id': task_id,
                            'agent_name': self.name,
                            'step_count': step_count,
                            'status': 'validation_complete',
                            'phase': 'validation',
                            'validation_complete': True,
                            'workflow_complete': is_complete,  # Indicate if workflow is complete
                            'validation_results': {
                                'passed': passed_count,
                                'failed': failed_count,
                                'total': total_checks,
                                'results': validation_results
                            }
                        }
                    )
        
        # Check if entire workflow is complete
        is_workflow_complete = (
            planning_complete and 
            generation_complete and 
            validation_complete
            # and deployment_approved  # Deployment is currently a placeholder
        )
        
        if not is_workflow_complete:
            return None
            
        # If helm generation workflow is complete, check for final messages
        messages = item.get("messages", [])
        last_message = messages[-1] if messages else None
        
        # Check if we should wait for the agent's final response
        if isinstance(last_message, ToolMessage):
            supervisor_logger.log_structured(
                level="INFO",
                message="Workflow state complete, waiting for agent final response",
                task_id=task_id,
                context_id=context_id
            )
            # Signal to continue loop to allow Agent node to execute and generate final text
            return "CONTINUE_LOOP"

        # If there is a final message from the agent, we might want to prioritize it
        # But this method is strictly for completion logic.
        # The caller handles yielding specific messages if needed, this returns the FINAL completion event.
        
        # Note: In the original logic, there was a check to yield last_message if it was text.
        # We can handle that by returning a list or handling it in the caller, but to keep it simple
        # and match the requested streamlining, we will return the completion response.
        # If the caller sees a valid response object, it yields it.
        
        # Actually, to EXACTLY semantic match the previous logic where it might YIELD TWICE
        # (once for text, once for completion), we should let the caller handle the text check
        # OR we package it all here. 
        # But the request is to "streamline".
        
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
        
        return AgentResponse(
            response_type='data',
            is_task_complete=True,
            require_user_input=False,
            content={
                "status": "completed",
                "message": "✅ Workflow complete: All phases finished and deployment approved.",
                "completion_metrics": {
                    'step_count': step_count,
                    'planning_complete': planning_complete,
                    'generation_complete': generation_complete,
                    'validation_complete': validation_complete,
                    'deployment_approved': deployment_approved
                }
            },
            metadata={
                'session_id': context_id,
                'task_id': task_id,
                'agent_name': self.name,
                'step_count': step_count,
                'status': 'completed'
            }
        )

    def _check_planning_completion(
        self,
        item: Dict[str, Any],
        task_id: str,
        context_id: str,
        step_count: int
    ) -> Optional[AgentResponse]:
        """
        Check if the Planning phase has completed and return the planning data response.
        This allows the UI to show planning artifacts before the full generation workflow completes.
        
        Args:
            item: Current state item from the graph stream
            task_id: Current task identifier
            context_id: Current context/session identifier
            step_count: Current step count in execution
            
        Returns:
            AgentResponse if planning is complete and data is available, None otherwise.
        """
        status = item.get('status')
        if status != 'completed':
            return None
            
        # Get planning data and status for individual agent completion
        is_complete, planning_status = self._validate_planner_agent_completion(item)
            
        if not is_complete:
            # Individual agent completed, but supervisor workflow not fully complete
            # We can log this but usually we just want to show "Processing..."
            return None
            
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
            
        return AgentResponse(
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

        return AgentResponse(
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

    def _prepare_graph_input(
        self,
        query_or_command: Union[str, Command],
        context_id: str,
        task_id: str
    ) -> Union[Dict[str, Any], Command]:
        """
        Prepare input for the graph execution, handling both new starts and resumes (Command).
        
        Args:
            query_or_command: Initial user query (str) or resume command (Command)
            context_id: Context/Thread identifier
            task_id: Task identifier
            
        Returns:
            Input for graph.astream() - either state dict or Command object
        """
        # Case 1: Start new conversation
        if not isinstance(query_or_command, Command):
            user_query = str(query_or_command)
            graph_input = self._create_initial_state(
                user_query=user_query,
                context_id=context_id,
                task_id=task_id
            )
            
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
            return graph_input

        # Case 2: Resume existing conversation (Command)
        thread_id = context_id
        try:
            checkpoint_state = self.compiled_graph.get_state(
                {"configurable": {"thread_id": thread_id}}
            )
            has_checkpoint = checkpoint_state is not None and checkpoint_state.values is not None
            has_interrupt = checkpoint_state and checkpoint_state.next and len(checkpoint_state.next) > 0
            
            if not has_checkpoint:
                # Critical Warning: Resuming without checkpoint
                supervisor_logger.log_structured(
                    level="ERROR",
                    message="Resume attempted but no checkpoint found - this should not happen after interrupt",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        "thread_id": thread_id,
                        "resume_value": str(query_or_command.resume)[:100] if query_or_command.resume else None,
                        "checkpoint_state": str(checkpoint_state)[:200] if checkpoint_state else None
                    }
                )
                supervisor_logger.log_structured(
                    level="WARNING",
                    message="Attempting resume without checkpoint - may cause issues",
                    task_id=task_id,
                    context_id=context_id,
                    extra={"thread_id": thread_id}
                )
                # Attempt to proceed with Command anyway, as creating new state creates duplicates
                return query_or_command
            else:
                # Checkpoint exists - normal resume
                resume_preview = None
                if query_or_command.resume is not None:
                    resume_str = str(query_or_command.resume)
                    resume_preview = resume_str[:200] if len(resume_str) > 200 else resume_str
                
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Resuming graph execution from checkpoint",
                    task_id=task_id,
                    context_id=context_id,
                    extra={
                        "thread_id": thread_id,
                        "resume_value_type": type(query_or_command.resume).__name__,
                        "resume_value_preview": resume_preview,
                        "has_resume_value": query_or_command.resume is not None,
                        "has_interrupt": has_interrupt,
                        "next_nodes": checkpoint_state.next if checkpoint_state else None
                    }
                )
                return query_or_command
                
        except Exception as e:
            supervisor_logger.log_structured(
                level="WARNING",
                message=f"Error checking checkpoint state, passing Command through: {e}",
                task_id=task_id,
                context_id=context_id,
                extra={"error": str(e), "thread_id": thread_id}
            )
            # CRITICAL FIX: Do NOT create new state on exception!
            # Just pass the Command through - LangGraph will handle it with checkpoint
            # Creating new state causes the workflow to restart from scratch
            return query_or_command

    @log_async
    async def stream(
        self,
        query_or_command,
        context_id: str,
        task_id: str,
        use_ui: bool = False
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

        # Prepare graph input (handles both new conversations and resumes)
        graph_input = self._prepare_graph_input(
            query_or_command=query_or_command,
            context_id=context_id,
            task_id=task_id
        )
        thread_id = context_id  # Ensure thread_id is consistent


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
        graph_stream = None
        try:
            # Use astream with values mode and subgraphs=True for proper handoff processing
            # Wrap with aclosing() for deterministic async generator cleanup
            # This prevents GeneratorExit exceptions during LangSmith trace writing
            async with isolation_shield():
                # Reverted aclosing usage as per user request to match previous stable state
                graph_stream = self.compiled_graph.astream(
                    graph_input, 
                    config_with_durability, 
                    stream_mode='values', 
                    subgraphs=True
                )
                async for item in graph_stream:
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
                            
                            # CRITICAL FIX: Exit the loop after interrupt!
                            # The graph is paused at the interrupt point.
                            # The client must resume with Command(resume=user_input)
                        

                        # 1.5 Check for final text response (model output text without tool calls)
                        # NOTE: This flows through using helper method
                        direct_text_response = self._handle_direct_model_text(
                            messages=item.get('messages', []),
                            task_id=task_id,
                            context_id=context_id,
                            step_count=step_count
                        )
                        if direct_text_response:
                            yield direct_text_response

                        

                        # 2. Check for Helm Management Agent completion (separate workflow from helm generation)
                        helm_mgmt_response = self._check_helm_mgmt_completion(
                            item=item,
                            task_id=task_id,
                            context_id=context_id,
                            step_count=step_count
                        )
                        if helm_mgmt_response:
                            yield helm_mgmt_response
                            

                        # 3. Check for Helm Generation workflow completion (planning/generation/validation)
                        # This also handles intermediate validation completion messages
                        helm_generation_result = self._check_helm_generation_completion(
                            item=item,
                            task_id=task_id,
                            context_id=context_id,
                            step_count=step_count
                        )
                        
                        if helm_generation_result == "CONTINUE_LOOP":
                            # Allow graph loop to continue for final agent response
                            pass 
                        elif helm_generation_result:
                            # It's a completion response (either validation phase or full workflow)
                            
                            # Check if it's a validation completion (intermediate) vs full workflow completion
                            is_validation_completion = (
                                helm_generation_result.metadata.get('status') == 'validation_complete'
                            )
                            
                            if not is_validation_completion:
                                # Full workflow completion - check for generic text message to yield BEFORE completion
                                messages = item.get("messages", [])
                                last_message = messages[-1] if messages else None
                                if isinstance(last_message, (AIMessage, HumanMessage)) and last_message.content and not isinstance(last_message, ToolMessage):
                                    supervisor_logger.log_structured(
                                        level="INFO",
                                        message="Yielding final agent message before completion",
                                        task_id=task_id,
                                        extra={"content_preview": str(last_message.content)[:50]}
                                    )
                                    yield AgentResponse(
                                        response_type='text',
                                        is_task_complete=False,
                                        require_user_input=False,
                                        content=str(last_message.content),
                                        metadata={
                                            'session_id': context_id,
                                            'task_id': task_id,
                                            'agent_name': self.name,
                                            'step_count': step_count,
                                            'status': 'working'
                                        }
                                    )
                            
                            yield helm_generation_result
                            continue

                        
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
                        

                        # 4. Check for intermediate Planning Phase completion
                        planning_phase_response = self._check_planning_completion(
                            item=item,
                            task_id=task_id,
                            context_id=context_id,
                            step_count=step_count
                        )
                        if planning_phase_response:
                            yield planning_phase_response
                            continue
                        
                        # 6. Default processing response - REMOVED
                        # We should not yield generic "Processing..." messages as they can overwrite
                        # useful status messages (like "Validation Complete") from previous steps
                        # if the graph emits additional internal events.
                        # If no handler has a specific response, we just wait for the next event.
                        pass
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
        finally:
            # Ensure all LangSmith traces are submitted before generator cleanup
            # This prevents GeneratorExit interrupting trace writing
            try:
                from langchain_core.tracers.langchain import wait_for_all_tracers
                wait_for_all_tracers()
                supervisor_logger.log_structured(
                    level="INFO",
                    message="Trace submission complete",
                    task_id=task_id,
                    context_id=context_id
                )
            except ImportError:
                # LangChain not available or old version
                pass
            except Exception as e:
                supervisor_logger.log_structured(
                    level="DEBUG",
                    message=f"Trace wait incomplete: {type(e).__name__}",
                    task_id=task_id,
                    context_id=context_id,
                    extra={"error": str(e)}
                )
            
            # Note: aclosing() automatically handles graph_stream.aclose(), no manual cleanup needed
            
            supervisor_logger.log_structured(
                level="INFO",
                message=f"[stream] END",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "agent_name": self.__class__.__name__,
                    "thread_id": thread_id,
                    "step_count": step_count,
                    "is_resume": isinstance(query_or_command, Command)
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