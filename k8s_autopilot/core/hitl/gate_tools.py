"""
HITL gate tools for supervisor integration.

These tools wrap HITL gates so they can be called by the supervisor agent
using the tool-based delegation pattern.
"""

from typing import List, Annotated
from langchain.tools import tool, ToolRuntime, InjectedToolCallId
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from k8s_autopilot.core.state.base import MainSupervisorState
from k8s_autopilot.core.hitl.gates import (
    planning_review_gate,
    generation_review_gate
)
from k8s_autopilot.core.hitl.utils import is_approved
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for gate tools
gate_tools_logger = AgentLogger("k8sAutopilotHITLGates")


@tool
async def request_planning_review(
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Request human review and approval of the planning output.
    
    Use this tool when:
    - Planning phase is complete (workflow_state.planning_complete == True)
    - Planning output exists in state (planning_output is not None)
    - Planning has not been approved yet (human_approval_status.planning.status != "approved")
    - You need to proceed to generation phase
    
    This tool will:
    1. Check if planning is already approved (if yes, returns immediately)
    2. Trigger an interrupt for human review
    3. Update approval status based on human decision
    4. Return status message
    
    Returns:
        Command: Command to update state with approval results
    """
    gate_tools_logger.log_structured(
        level="INFO",
        message="Planning review gate tool invoked",
        extra={"state_keys": list(runtime.state.keys())}
    )
    
    result_message = ""
    
    # Check if already approved
    if is_approved(runtime.state, "planning"):
        approval = runtime.state.get("human_approval_status", {}).get("planning")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        result_message = f"✅ Planning already approved by {reviewer or 'previous reviewer'}. Proceeding to generation phase."
        return Command(
            update={
                "messages": [ToolMessage(content=result_message, tool_call_id=tool_call_id)]
            }
        )
    
    # Check if planning output exists
    planning_output = runtime.state.get("planning_output")
    if not planning_output:
        result_message = "⚠️ No planning output available for review. Please complete planning phase first."
        return Command(
            update={
                "messages": [ToolMessage(content=result_message, tool_call_id=tool_call_id)]
            }
        )
    
    # Call the gate function
    try:
        gate_result = planning_review_gate(runtime.state)
        
        # Prepare state updates
        update_dict = {}
        
        if "human_approval_status" in gate_result:
            # We need to merge intelligently or just let LangGraph merge if it's a dict
            # For robustness, let's look at current state
             current_approvals = runtime.state.get("human_approval_status") or {}
             new_approvals = gate_result["human_approval_status"]
             # Merge new into current
             merged_approvals = {**current_approvals, **new_approvals}
             update_dict["human_approval_status"] = merged_approvals
        
        # Extract messages from gate result (usually AIMessage from interrupt)
        # We don't necessarily want to append them as is, or maybe we do?
        # Gate usually returns AIMessage with the result of the review
        gate_messages = gate_result.get("messages", [])
        
        # Extract approval status
        approval = gate_result.get("human_approval_status", {}).get("planning")
        if approval:
            status = approval.status if hasattr(approval, "status") else approval.get("status", "pending")
            reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer")
            
            if status == "approved":
                result_message = f"✅ Planning approved by {reviewer or 'reviewer'}. You can now proceed to generation phase."
            elif status == "rejected":
                result_message = f"❌ Planning rejected by {reviewer or 'reviewer'}. Please revise the planning or end the workflow."
            elif status == "modified":
                result_message = f"✏️ Planning modified by {reviewer or 'reviewer'}. Please review changes and proceed."
            else:
                result_message = f"⏳ Planning review pending. Waiting for human approval..."
        else:
             result_message = "⏳ Planning review initiated. Waiting for human approval..."
        
        # Add ToolMessage
        tool_message = ToolMessage(content=result_message, tool_call_id=tool_call_id)
        
        # Messages creation: 
        # gate_messages from inner generic_review_gate might contain AIMessage with decision
        # We can include them or just the tool message. 
        # Including just ToolMessage is safer for request/response flow.
        update_dict["messages"] = [tool_message]
        
        return Command(update=update_dict)
        
    except Exception as e:
        gate_tools_logger.log_structured(
            level="ERROR",
            message=f"Error in planning review gate: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        error_msg = f"❌ Error requesting planning review: {str(e)}"
        return Command(
            update={
                "messages": [ToolMessage(content=error_msg, tool_call_id=tool_call_id)]
            }
        )


@tool
async def request_generation_review(
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Request human review of generated artifacts and obtain workspace directory.
    
    Use this tool when:
    - Generation phase is complete (workflow_state.generation_complete == True)
    - Generated artifacts exist in state
    - Generation has not been approved yet (human_approval_status.generation.status != "approved")
    - You need to proceed to validation phase
    
    This tool will:
    1. Check if generation is already approved
    2. Show generated artifacts summary
    3. Request workspace directory configuration
    4. Trigger interrupt for human review
    5. Update approval status and workspace_dir
    
    Returns:
        Command: Command to update state with approval results and workspace_dir
    """
    gate_tools_logger.log_structured(
        level="INFO",
        message="Generation review gate tool invoked",
        extra={"state_keys": list(runtime.state.keys())}
    )
    
    result_message = ""
    
    # Check if already approved
    if is_approved(runtime.state, "generation"):
        approval = runtime.state.get("human_approval_status", {}).get("generation")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        result_message = f"✅ Generation already approved by {reviewer or 'previous reviewer'}. Proceeding to validation phase."
        return Command(
            update={
                "messages": [ToolMessage(content=result_message, tool_call_id=tool_call_id)]
            }
        )
    
    # Check if artifacts exist
    helm_chart_artifacts = runtime.state.get("helm_chart_artifacts", {})
    if not helm_chart_artifacts:
        result_message = "⚠️ No helm chart artifacts available for review. Please complete generation phase first."
        return Command(
             update={
                "messages": [ToolMessage(content=result_message, tool_call_id=tool_call_id)]
            }
        )
    
    # Call the gate function
    try:
        gate_result = generation_review_gate(runtime.state)
        
        update_dict = {}
        
        # Update runtime state with gate result
        if "human_approval_status" in gate_result:
             current_approvals = runtime.state.get("human_approval_status") or {}
             new_approvals = gate_result["human_approval_status"]
             merged_approvals = {**current_approvals, **new_approvals}
             update_dict["human_approval_status"] = merged_approvals
        
        # Update workspace_dir if provided in gate result
        if "workspace_dir" in gate_result:
            update_dict["workspace_dir"] = gate_result["workspace_dir"]
            gate_tools_logger.log_structured(
                level="INFO",
                message="Workspace directory set from generation review",
                extra={"workspace_dir": gate_result["workspace_dir"]}
            )
        
        # Extract approval status
        approval = gate_result.get("human_approval_status", {}).get("generation")
        workspace_dir = gate_result.get("workspace_dir", "/tmp/helm-charts")
        
        # Get raw comments again to include in the tool output message
        # This is CRITICAL so the Supervisor Agent can "hear" what the user said
        # e.g. "perfect, use /my/path"
        raw_comments = gate_result.get("messages", [{}])[0].content if gate_result.get("messages") else ""
        # Or cleaner: retrieve from approval object if available, or just use what we merged
        
        if approval:
            status = approval.status if hasattr(approval, "status") else approval.get("status", "pending")
            reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer")
            
            # Construct a rich status message that includes the user's raw feedback/intent
            if status == "approved":
                result_message = f"✅ Generation approved by {reviewer or 'reviewer'}. Resulting Workspace: {workspace_dir}. User comments: '{gate_messages[0].content if gate_messages else 'Approved'}'"
            elif status == "rejected":
                result_message = f"❌ Generation review rejected by {reviewer or 'reviewer'}. User comments: '{gate_messages[0].content if gate_messages else 'Rejected'}'"
            else:
                result_message = f"⏳ Generation review pending. Waiting for human approval..."
        else:
             result_message = "⏳ Generation review initiated. Waiting for human approval..."
        
        # Add ToolMessage
        tool_message = ToolMessage(content=result_message, tool_call_id=tool_call_id)
        update_dict["messages"] = [tool_message]
        
        return Command(update=update_dict)
        
    except Exception as e:
        gate_tools_logger.log_structured(
            level="ERROR",
            message=f"Error in generation review gate: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        error_msg = f"❌ Error requesting generation review: {str(e)}"
        return Command(
            update={
                "messages": [ToolMessage(content=error_msg, tool_call_id=tool_call_id)]
            }
        )


# Define the list of available gate tools
def create_hitl_gate_tools() -> List:
    """
    Create list of HITL gate tools for supervisor integration.
    """
    return [
        request_planning_review,
        request_generation_review
    ]


