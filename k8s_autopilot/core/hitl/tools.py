"""Unified HITL tools — single entry point for all human interaction.

All coordinators use these tools. Policies loaded from /memories/hitl-policies.md.
Uses deepagents interrupt_on config for tool-level gates.
Docs: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
"""

from typing import Optional, Annotated, Any, TYPE_CHECKING
from datetime import datetime, timezone
from langchain.tools import tool, InjectedToolCallId
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
else:
    ToolRuntime = Any
from langgraph.types import Command, interrupt
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("HITLTools")


@tool
def request_human_input(
    question: str,
    context: Optional[str] = None,
    phase: Optional[str] = None,
    runtime: Optional[ToolRuntime] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Request human input during workflow execution.
    
    This is the SINGLE HITL tool for all coordinators.
    The interrupt payload is standardized for A2UI rendering.
    """
    current_phase = phase
    if runtime and runtime.state and not current_phase:
        current_phase = (
            runtime.state.get("current_phase")
            if isinstance(runtime.state, dict)
            else getattr(runtime.state, "current_phase", "unknown")
        )
    


    payload = {
        "question": question,
        "context": context or "",
        "phase": current_phase or "unknown",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    logger.info(f"HITL interrupt: phase={current_phase}, question={question[:80]}")
    
    human_response = interrupt(payload)
    human_response_str = str(human_response) if human_response else ""
    
    return Command(update={
        "messages": [ToolMessage(
            content=f"User responded: {human_response_str}",
            tool_call_id=tool_call_id,
        )],
    })


def create_hitl_tools():
    """Return list of HITL tools for supervisor/coordinator tool lists."""
    return [request_human_input]
