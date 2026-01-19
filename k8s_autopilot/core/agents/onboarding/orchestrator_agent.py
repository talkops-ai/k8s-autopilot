"""
ArgoCD Application Onboarding Orchestrator Agent

This is the main Deep Agent that orchestrates ArgoCD application onboarding
using the create_deep_agent pattern with sub-agents for specialized tasks.

Architecture References:
- docs/app_onboarding/K8s-Autopilot-Deep-Agent-Architecture.md
- k8s_autopilot/core/agents/helm_mgmt/helm_mgmt_agent.py
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set, Annotated
from pydantic import BaseModel

from langgraph.types import Command, interrupt
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent, CompiledSubAgent
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware, HumanInTheLoopMiddleware
from langchain.chat_models import init_chat_model
from langchain.tools import tool, InjectedToolCallId, ToolRuntime


from k8s_autopilot.core.agents.base_agent import BaseSubgraphAgent
from k8s_autopilot.core.state.base import ArgoCDOnboardingState
from k8s_autopilot.core.agents.onboarding.prompts import (
    ARGOCD_ORCHESTRATOR_PROMPT,
    PROJECT_AGENT_PROMPT,
    REPOSITORY_AGENT_PROMPT,
    APPLICATION_AGENT_PROMPT,
    DEBUG_AGENT_PROMPT,
)
from k8s_autopilot.core.agents.onboarding.middleware import (
    ArgoCDStateMiddleware,
    ArgoCDApprovalHITLMiddleware,
    ArgoCDMissingInputsHITLMiddleware,
    ArgoCDErrorRecoveryMiddleware,
)
from k8s_autopilot.utils.mcp_client import MCPAdapterClient
from k8s_autopilot.config.config import Config
from k8s_autopilot.utils.logger import AgentLogger

# Agent logger
argocd_onboarding_logger = AgentLogger("k8sAutopilotArgoCDOnboardingAgent")


# ============================================================================
# HITL Tool for requesting human input (Plan review checkpoint)
# ============================================================================

@tool
def request_human_input(
    question: str,
    context: Optional[str] = None,
    phase: Optional[str] = None,
    runtime: ToolRuntime[None, ArgoCDOnboardingState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """
    Request human input during ArgoCD workflow execution.

    **IMPORTANT:** This tool does NOT interpret or classify the human response.
    It only pauses execution (via `interrupt(...)`) and returns the raw response
    as a ToolMessage so the deep agent can decide what to do next.
    """
    # Best-effort: derive phase/session for UI payload
    if runtime and runtime.state:
        session_id = getattr(runtime.state, "session_id", "unknown")
        if not phase:
            phase = str(getattr(runtime.state, "current_phase", "plan_review"))
    else:
        session_id = "unknown"
        phase = phase or "discovery"

    pending_payload: Dict[str, Any] = {
        "status": "input_required",
        "session_id": session_id,
        "question": question,
        "context": context or "No additional context provided",
        "active_phase": phase,
        "tool_name": "request_human_input",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    interrupt_payload = {"pending_feedback_requests": pending_payload}

    argocd_onboarding_logger.log_structured(
        level="INFO",
        message="Requesting human input",
        extra={
            "phase": phase,
            "session_id": session_id,
            "question_preview": question[:120] if isinstance(question, str) else str(question)[:120],
        },
    )

    human_response = interrupt(interrupt_payload)
    human_response_str = str(human_response) if human_response is not None else ""

    tool_message = ToolMessage(
        content=f"Human has responded: {human_response_str}",
        tool_call_id=tool_call_id,
    )

    return Command(
        update={
            "messages": [tool_message],
        }
    )


class ArgoCDOnboardingAgent(BaseSubgraphAgent):
    """
    ArgoCD Application Onboarding Deep Agent.
    
    Implements the Deep Agent pattern with specialized sub-agents:
    - ProjectAgent: Project CRUD operations
    - RepositoryAgent: Repo validation/onboarding (HTTPS/SSH)
    - ApplicationAgent: App lifecycle management
    - DebugAgent: Troubleshooting and diagnostics
    
    The supervisor uses create_deep_agent to intelligently delegate to sub-agents.
    """
    
    # Tool names for each sub-agent category
    PROJECT_TOOL_NAMES: Set[str] = {
        "create_project", "get_project", "update_project", 
        "delete_project", "list_projects"
    }
    
    REPO_TOOL_NAMES: Set[str] = {
        "validate_repository_connection", "onboard_repository_https",
        "onboard_repository_ssh", "list_repositories",
        "get_repository", "delete_repository"
    }
    
    APP_TOOL_NAMES: Set[str] = {
        "create_application", "get_application_details",
        "update_application", "delete_application",
        "sync_application",
        # Optional validation tool (if exposed by MCP server)
        "validate_application_config",
        # Query/preview tools needed for intelligent orchestration
        "list_applications",
        "get_application_diff",
        "get_sync_status",
    }
    
    DEBUG_TOOL_NAMES: Set[str] = {
        "get_application_logs",
        "get_application_events",
    }
    
    # Supervisor tools (high-risk operations that need HITL)
    SUPERVISOR_TOOL_NAMES: Set[str] = {
        "sync_application",
        "delete_application",
        "delete_project",
    }
    
    def __init__(
        self,
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        name: str = "argocd_onboarding_agent",
        memory: Optional[MemorySaver] = None,
    ):
        """Initialize the ArgoCD Onboarding Agent."""
        self.config_instance = config or Config(custom_config or {})
        argocd_onboarding_logger.log_structured(
            level="INFO",
            message="Initializing ArgoCDOnboardingAgent",
            extra={"name": name}
        )
        
        self._name = name
        self.memory = memory or MemorySaver()
        
        # MCP client for ArgoCD tools ONLY (filter to argocd_mcp_server)
        self._mcp_client = MCPAdapterClient(
            config=self.config_instance,
            server_filter=['argocd_mcp_server']  # Only connect to ArgoCD server
        )
        self._mcp_tools: List[BaseTool] = []
        self._mcp_resources: Dict[str, Any] = {}
        self._mcp_initialized = False
        
        # Sub-agents (initialized in build_graph)
        self._sub_agents: List[CompiledSubAgent] = []
        
        # Deep agent (created in build_graph)
        self.argocd_onboarding_agent = None
        
        # Initialize LLM
        self._initialize_llm()
        
        argocd_onboarding_logger.log_structured(
            level="INFO",
            message="ArgoCDOnboardingAgent initialized"
        )
    
    def _initialize_llm(self) -> None:
        """Initialize LLM models."""
        try:
            llm_config = self.config_instance.get_llm_config()
            config_for_init = {k: v for k, v in llm_config.items() if k != 'provider'}
            self.model = init_chat_model(**config_for_init)
            
            # Use same or different model for deep agent
            llm_deepagent_config = self.config_instance.get_llm_deepagent_config()
            deepagent_config = {k: v for k, v in llm_deepagent_config.items() if k != 'provider'}
            self.deep_agent_model = init_chat_model(**deepagent_config)
            
            argocd_onboarding_logger.log_structured(
                level="INFO",
                message=f"LLMs initialized: {llm_config.get('model', 'unknown')}"
            )
        except Exception as e:
            argocd_onboarding_logger.log_structured(
                level="ERROR",
                message=f"Failed to initialize LLM: {e}"
            )
            raise
    
    async def initialize_mcp(self) -> None:
        """Initialize MCP client and load tools."""
        if self._mcp_initialized:
            return
        
        try:
            await self._mcp_client.initialize()
            self._mcp_tools = self._mcp_client.get_tools()
            self._mcp_initialized = True
            
            argocd_onboarding_logger.log_structured(
                level="INFO",
                message="MCP integration initialized",
                extra={"tool_count": len(self._mcp_tools)}
            )
        except Exception as e:
            argocd_onboarding_logger.log_structured(
                level="WARNING",
                message=f"MCP init failed (degraded mode): {e}"
            )
            self._mcp_initialized = True
    
    def _filter_tools_by_names(self, tool_names: Set[str]) -> List[BaseTool]:
        """Filter loaded tools by name."""
        return [t for t in self._mcp_tools if t.name in tool_names]
    
    def _initialize_sub_agents(self) -> None:
        """
        Initialize specialized sub-agents with filtered tools.
        
        Each sub-agent:
        1. Gets its own filtered set of MCP tools
        2. Has its own middleware stack
        3. Is wrapped in CompiledSubAgent for the supervisor
        """
        # Resource tool for reading MCP resources
        resource_tool = self._create_resource_tool()
        
        # ========== Project Agent ==========
        project_tools = self._filter_tools_by_names(self.PROJECT_TOOL_NAMES)
        
        project_agent = create_agent(
            model=self.model,
            system_prompt=PROJECT_AGENT_PROMPT,
            tools=project_tools + [resource_tool],
            state_schema=ArgoCDOnboardingState,
            middleware=[
                ArgoCDStateMiddleware(),
                ArgoCDMissingInputsHITLMiddleware(),
                # Use ArgoCDApprovalHITLMiddleware for standardized approval templates
                ArgoCDApprovalHITLMiddleware(),  # Consistent HITL behavior across sub-agents
                ArgoCDErrorRecoveryMiddleware(retry_tools={"get_project", "list_projects"}),
            ],
        )
        
        self.project_subagent = CompiledSubAgent(
            name="project_agent",
            description="Specialized agent for ArgoCD project CRUD operations. Use for creating, listing, updating, or deleting ArgoCD projects.",
            runnable=project_agent,
        )
        
        # ========== Repository Agent ==========
        repo_tools = self._filter_tools_by_names(self.REPO_TOOL_NAMES)
        
        repo_agent = create_agent(
            model=self.model,
            system_prompt=REPOSITORY_AGENT_PROMPT,
            tools=repo_tools + [resource_tool],
            state_schema=ArgoCDOnboardingState,
            middleware=[
                ArgoCDStateMiddleware(),
                ArgoCDMissingInputsHITLMiddleware(),
                HumanInTheLoopMiddleware(
                    interrupt_on={"delete_repository": True},
                    description_prefix="Approval required for tool execution"
                ),
                ArgoCDApprovalHITLMiddleware(),  # Consistent HITL behavior across sub-agents
                ArgoCDErrorRecoveryMiddleware(retry_tools={"validate_repository_connection", "list_repositories"}),
            ],
        )
        
        self.repo_subagent = CompiledSubAgent(
            name="repository_agent",
            description="Specialized agent for repository validation and onboarding. Use for validating repo connections, onboarding repos via HTTPS or SSH.",
            runnable=repo_agent,
        )
        
        # ========== Application Agent ==========
        app_tools = self._filter_tools_by_names(self.APP_TOOL_NAMES)
        
        app_agent = create_agent(
            model=self.model,
            system_prompt=APPLICATION_AGENT_PROMPT,
            tools=app_tools + [resource_tool],
            state_schema=ArgoCDOnboardingState,
            middleware=[
                ArgoCDStateMiddleware(),
                ArgoCDMissingInputsHITLMiddleware(),
                # Use ArgoCDApprovalHITLMiddleware for standardized approval templates
                ArgoCDApprovalHITLMiddleware(),  # Production gating only
                ArgoCDErrorRecoveryMiddleware(retry_tools={"get_application_details", "get_application_status"}),
            ],
        )
        
        self.app_subagent = CompiledSubAgent(
            name="application_agent",
            description="Specialized agent for ArgoCD application lifecycle. Use for creating, syncing, updating, or getting status of applications.",
            runnable=app_agent,
        )
        
        # ========== Debug Agent ==========
        debug_tools = self._filter_tools_by_names(self.DEBUG_TOOL_NAMES)
        
        debug_agent = create_agent(
            model=self.model,
            system_prompt=DEBUG_AGENT_PROMPT,
            tools=debug_tools + [resource_tool],
            state_schema=ArgoCDOnboardingState,
            middleware=[
                ArgoCDStateMiddleware(),
                ArgoCDErrorRecoveryMiddleware(),  # All debug tools can be retried
            ],
        )
        
        self.debug_subagent = CompiledSubAgent(
            name="debug_agent",
            description="Specialized agent for troubleshooting and diagnostics. Use for collecting logs, events, metrics, and analyzing application issues.",
            runnable=debug_agent,
        )
        
        # Collect all sub-agents for the supervisor
        self._sub_agents = [
            self.project_subagent,
            self.repo_subagent,
            self.app_subagent,
            self.debug_subagent,
        ]
        
        argocd_onboarding_logger.log_structured(
            level="INFO",
            message="Sub-agents initialized",
            extra={
                "project_tools": len(project_tools),
                "repo_tools": len(repo_tools),
                "app_tools": len(app_tools),
                "debug_tools": len(debug_tools),
            }
        )
    
    def _create_resource_tool(self) -> BaseTool:
        """Create a tool that allows agents to read MCP resources by URI."""
        
        async def read_mcp_resource(uri: str) -> str:
            """
            Read a specific resource from the ArgoCD MCP server.
            
            Available resources:
            - argocd://projects - List all projects
            - argocd://projects/{name} - Get project details
            - argocd://repositories - List all repositories
            - argocd://applications - List all applications
            - argocd://applications/{name} - Get application details
            - argocd://applications/{name}/status - Get application status
            
            Args:
                uri: The full URI of the resource to read.
            """
            try:
                resources = await self._mcp_client.get_resources(
                    uris=(uri,),
                    server_name='argocd_mcp_server'
                )
                if not resources:
                    return f"Resource not found: {uri}"
                
                res = resources[0]
                
                # Extract text content from ReadResourceResult
                if hasattr(res, 'contents') and res.contents:
                    for content_item in res.contents:
                        if hasattr(content_item, 'text'):
                            return content_item.text

                return str(res)
            except Exception as e:
                return f"Error reading resource {uri}: {str(e)}"

        return StructuredTool.from_function(
            func=None,
            coroutine=read_mcp_resource,
            name="read_mcp_resource",
            description="Read content of a specific MCP resource (e.g., project details, application status)."
        )
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def state_model(self) -> type[BaseModel]:
        return ArgoCDOnboardingState
    
    def build_graph(self) -> StateGraph:
        """
        Build the deep agent graph with sub-agent delegation.

        HITL for missing required inputs is enforced via ArgoCDMissingInputsHITLMiddleware
        (tool-call review/edit) rather than hard-coding a separate workflow graph.
        """
        # Initialize sub-agents with their tools
        self._initialize_sub_agents()

        # Supervisor gets execution tools (high-risk operations with HITL)
        supervisor_tools = self._filter_tools_by_names(self.SUPERVISOR_TOOL_NAMES)

        # Add resource tool + plan review tool
        resource_tool = self._create_resource_tool()
        supervisor_tools = supervisor_tools + [resource_tool, request_human_input]

        # Create deep agent - supervisor will delegate to sub-agents
        self.argocd_onboarding_agent = create_deep_agent(
            model=self.deep_agent_model,
            system_prompt=ARGOCD_ORCHESTRATOR_PROMPT,
            tools=supervisor_tools,
            subagents=self._sub_agents,
            checkpointer=self.memory,
            context_schema=ArgoCDOnboardingState,
            middleware=[
                ArgoCDStateMiddleware(),
                ArgoCDMissingInputsHITLMiddleware(),  # deterministic missing-arg HITL
                ArgoCDApprovalHITLMiddleware(),
                ArgoCDErrorRecoveryMiddleware(),
            ],
        )

        argocd_onboarding_logger.log_structured(
            level="INFO",
            message="Deep agent graph built",
            extra={"subagent_count": len(self._sub_agents)},
        )

        return self.argocd_onboarding_agent
    
    async def close(self) -> None:
        """Close the MCP client connection."""
        if self._mcp_client:
            await self._mcp_client.close()
            self._mcp_initialized = False


# ============================================================================
# Factory Functions
# ============================================================================

async def create_argocd_onboarding_agent(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "argocd_onboarding_agent",
    memory: Optional[MemorySaver] = None,
) -> ArgoCDOnboardingAgent:
    """
    Factory to create and initialize ArgoCD Onboarding Agent.
    
    Note: MCP client is initialized but NOT closed here.
    Call agent.close() explicitly when done.
    """
    agent = ArgoCDOnboardingAgent(
        config=config,
        custom_config=custom_config,
        name=name,
        memory=memory,
    )
    await agent.initialize_mcp()
    return agent


async def create_argocd_onboarding_agent_factory(
    config: Config
) -> ArgoCDOnboardingAgent:
    """Factory function for use with dependency injection."""
    return await create_argocd_onboarding_agent(config=config)


# Export for package
__all__ = [
    "ArgoCDOnboardingAgent",
    "create_argocd_onboarding_agent",
    "create_argocd_onboarding_agent_factory",
]