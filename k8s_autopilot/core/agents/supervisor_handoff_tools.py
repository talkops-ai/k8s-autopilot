from typing import Annotated, Dict, Any, Optional
from datetime import datetime, timezone
from langchain.tools import tool, BaseTool, InjectedToolCallId
from langchain_core.messages import ToolMessage, HumanMessage
from langgraph.types import Command, Send
from langgraph.prebuilt import InjectedState
from langgraph_supervisor.handoff import METADATA_KEY_HANDOFF_DESTINATION
from k8s_autopilot.core.state.state_transformer import StateTransformer
from k8s_autopilot.utils.logger import AgentLogger

handoff_logger = AgentLogger("k8sAutopilotSupervisorHandoffTools")

def create_handoff_tool_for_agent(agent_name: str, name: str | None = None, description: str | None = None) -> BaseTool:
    """
    Create a custom handoff tool that passes session_id and task_id to agents.
    
    Args:
        agent_name: Name of the target agent
        name: Tool name (if None, will be auto-generated)
        description: Tool description (if None, will be auto-generated)
        
    Returns:
        BaseTool: Custom handoff tool
    """
    # Auto-generate name and description if not provided
    if name is None:
        name = f"transfer_to_{agent_name}"
    if description is None:
        description = f"Transfer task to {agent_name}"

    # Create the tool function with proper docstring using @tool decorator
    @tool(name, description=description)
    def handoff_to_agent(
        state: Annotated[Any, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
        task_description: Annotated[str, "Description of what the next agent should do"] = "",
        ):
        """
        Handoff to a specific agent with session_id and task_id.
        
        This tool:
        1. Extracts session_id and task_id from the supervisor state
        2. Creates a tool message for the handoff
        3. Returns a Command to transfer control to the target agent
        4. Passes all necessary context including session_id and task_id
        
        Args:
            state: Current supervisor state (injected)
            tool_call_id: Tool call identifier (injected)
            task_description: Optional description for the handoff
        """
        # Extract user query from state if task_description is empty
        if not task_description:
            task_description = state.get("user_query", "Continue workflow")

        handoff_logger.log_structured(
            level="INFO",
            message=f"Handoff tool called for {agent_name}",
            extra={
                "agent_name": agent_name,
                "task_description": task_description,
                "state_keys": list(state.keys()) if isinstance(state, dict) else "not_dict",
                "state_type": type(state).__name__,
            }
        )
        # Try to extract from state directly first
        session_id = state.get("session_id")
        task_id = state.get("task_id")
        user_request = state.get("user_request", "")
        try:
            tool_message = ToolMessage(
                content=f"Successfully transferred to {agent_name}",
                name=name,
                tool_call_id=tool_call_id,
            )

        except Exception as e:
            handoff_logger.log_structured(
                level="ERROR",
                message=f"Failed to create ToolMessage for {agent_name}",
                extra={
                    "agent_name": agent_name,
                    "tool_call_id": tool_call_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )
            # Fallback: create a simple message without tool_call_id
            tool_message = ToolMessage(
                content=f"Successfully transferred to {agent_name}",
                name=name,
                tool_call_id=tool_call_id,
            )
        # Build state update with messages and handoff context
        # State transformation happens in wrapper nodes, not here!
        state_update = {
            "messages": [tool_message],  # MUST include ToolMessage (LangChain requirement)
            "handoff_context": {
                "task_description": task_description,
                "target_agent": agent_name,
                "handoff_time": datetime.now(timezone.utc).isoformat()
            }
        }
        
        handoff_logger.log_structured(
            level="INFO",
            message=f"Handoff tool returning state update for {agent_name}",
            extra={
                "agent_name": agent_name,
                "state_update_keys": list(state_update.keys()),
                "has_messages": "messages" in state_update
            }
        )
        
        # Return Command with state update only
        # Routing is handled by create_supervisor via metadata
        return Command(
            update=state_update,
            goto=agent_name,
            graph=Command.PARENT
        )

    
    # Set metadata for langgraph-supervisor
    handoff_to_agent.metadata = {METADATA_KEY_HANDOFF_DESTINATION: agent_name}
    
    return handoff_to_agent

def create_handoff_tools_for_agents(agent_names: list[str]) -> list[BaseTool]:
    """
    Create handoff tools for multiple agents.
    
    Args:
        agent_names: List of agent names to create handoff tools for
        
    Returns:
        List[BaseTool]: List of handoff tools
    """
    tools = []
    
    for agent_name in agent_names:
        tool = create_handoff_tool_for_agent(
            agent_name=agent_name,
            name=None,  # Auto-generate name
            description=None  # Auto-generate description
        )
        tools.append(tool)
        
        handoff_logger.log_structured(
            level="INFO",
            message=f"Created handoff tool for {agent_name}",
            extra={
                "agent_name": agent_name,
                "tool_name": tool.name,
                "tool_description": tool.description,
            }
        )
    
    return tools