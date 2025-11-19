"""
HITL gate tools for supervisor integration.

These tools wrap HITL gates so they can be called by the supervisor agent
using the tool-based delegation pattern.
"""

from typing import List
from langchain.tools import tool, ToolRuntime
from k8s_autopilot.core.state.base import MainSupervisorState
from k8s_autopilot.core.hitl.gates import (
    planning_review_gate,
    security_review_gate,
    deployment_approval_gate
)
from k8s_autopilot.core.hitl.utils import is_approved
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for gate tools
gate_tools_logger = AgentLogger("k8sAutopilotHITLGates")


@tool
async def request_planning_review(
    runtime: ToolRuntime[None, MainSupervisorState]
) -> str:
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
        Status message indicating approval result
    """
    gate_tools_logger.log_structured(
        level="INFO",
        message="Planning review gate tool invoked",
        extra={"state_keys": list(runtime.state.keys())}
    )
    
    # Check if already approved
    if is_approved(runtime.state, "planning"):
        approval = runtime.state.get("human_approval_status", {}).get("planning")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return f"✅ Planning already approved by {reviewer or 'previous reviewer'}. Proceeding to generation phase."
    
    # Check if planning output exists
    planning_output = runtime.state.get("planning_output")
    if not planning_output:
        return "⚠️ No planning output available for review. Please complete planning phase first."
    
    # Call the gate function
    try:
        gate_result = planning_review_gate(runtime.state)
        
        # Update runtime state with gate result
        if "human_approval_status" in gate_result:
            if "human_approval_status" not in runtime.state:
                runtime.state["human_approval_status"] = {}
            runtime.state["human_approval_status"].update(gate_result["human_approval_status"])
        
        if "messages" in gate_result:
            # Add messages to state
            if "messages" not in runtime.state:
                runtime.state["messages"] = []
            runtime.state["messages"].extend(gate_result["messages"])
        
        # Extract approval status
        approval = gate_result.get("human_approval_status", {}).get("planning")
        if approval:
            status = approval.status if hasattr(approval, "status") else approval.get("status", "pending")
            reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer")
            
            if status == "approved":
                return f"✅ Planning approved by {reviewer or 'reviewer'}. You can now proceed to generation phase."
            elif status == "rejected":
                return f"❌ Planning rejected by {reviewer or 'reviewer'}. Please revise the planning or end the workflow."
            elif status == "modified":
                return f"✏️ Planning modified by {reviewer or 'reviewer'}. Please review changes and proceed."
            else:
                return f"⏳ Planning review pending. Waiting for human approval..."
        
        return "⏳ Planning review initiated. Waiting for human approval..."
        
    except Exception as e:
        gate_tools_logger.log_structured(
            level="ERROR",
            message=f"Error in planning review gate: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        return f"❌ Error requesting planning review: {str(e)}"


@tool
async def request_security_review(
    runtime: ToolRuntime[None, MainSupervisorState]
) -> str:
    """
    Request human review and approval of security scan results.
    
    Use this tool when:
    - Generation phase is complete (workflow_state.generation_complete == True)
    - Validation results exist (validation_results is not empty)
    - Security has not been approved yet (human_approval_status.security.status != "approved")
    - You need to proceed to validation/deployment phase
    
    This tool will:
    1. Check if security is already approved (if yes, returns immediately)
    2. Extract security issues from validation results
    3. Trigger an interrupt for human review
    4. Update approval status based on human decision
    5. Return status message
    
    Returns:
        Status message indicating approval result
    """
    gate_tools_logger.log_structured(
        level="INFO",
        message="Security review gate tool invoked",
        extra={"state_keys": list(runtime.state.keys())}
    )
    
    # Check if already approved
    if is_approved(runtime.state, "security"):
        approval = runtime.state.get("human_approval_status", {}).get("security")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return f"✅ Security already approved by {reviewer or 'previous reviewer'}. Proceeding to validation phase."
    
    # Check if validation results exist
    validation_results = runtime.state.get("validation_results", [])
    if not validation_results:
        return "⚠️ No validation results available for security review. Please complete generation and validation first."
    
    # Call the gate function
    try:
        gate_result = security_review_gate(runtime.state)
        
        # Update runtime state with gate result
        if "human_approval_status" in gate_result:
            if "human_approval_status" not in runtime.state:
                runtime.state["human_approval_status"] = {}
            runtime.state["human_approval_status"].update(gate_result["human_approval_status"])
        
        if "messages" in gate_result:
            if "messages" not in runtime.state:
                runtime.state["messages"] = []
            runtime.state["messages"].extend(gate_result["messages"])
        
        # Extract approval status
        approval = gate_result.get("human_approval_status", {}).get("security")
        if approval:
            status = approval.status if hasattr(approval, "status") else approval.get("status", "pending")
            reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer")
            
            if status == "approved":
                return f"✅ Security approved by {reviewer or 'reviewer'}. You can now proceed to validation/deployment phase."
            elif status == "rejected":
                return f"❌ Security review rejected by {reviewer or 'reviewer'}. Please fix security issues or end the workflow."
            else:
                return f"⏳ Security review pending. Waiting for human approval..."
        
        return "⏳ Security review initiated. Waiting for human approval..."
        
    except Exception as e:
        gate_tools_logger.log_structured(
            level="ERROR",
            message=f"Error in security review gate: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        return f"❌ Error requesting security review: {str(e)}"


@tool
async def request_deployment_approval(
    runtime: ToolRuntime[None, MainSupervisorState]
) -> str:
    """
    Request final human approval before deploying to cluster.
    
    Use this tool when:
    - Validation phase is complete (workflow_state.validation_complete == True)
    - All validations have passed
    - Generated artifacts exist (generated_artifacts is not empty)
    - Deployment has not been approved yet (human_approval_status.deployment.status != "approved")
    - You are ready to complete the workflow
    
    This tool will:
    1. Check if deployment is already approved (if yes, returns immediately)
    2. Prepare deployment summary with validation results
    3. Trigger an interrupt for final human approval
    4. Update approval status based on human decision
    5. Return status message
    
    Returns:
        Status message indicating approval result
    """
    gate_tools_logger.log_structured(
        level="INFO",
        message="Deployment approval gate tool invoked",
        extra={"state_keys": list(runtime.state.keys())}
    )
    
    # Check if already approved
    if is_approved(runtime.state, "deployment"):
        approval = runtime.state.get("human_approval_status", {}).get("deployment")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return f"✅ Deployment already approved by {reviewer or 'previous reviewer'}. Workflow complete."
    
    # Check if validation is complete
    validation_results = runtime.state.get("validation_results", [])
    generated_artifacts = runtime.state.get("generated_artifacts", {})
    
    if not validation_results and not generated_artifacts:
        return "⚠️ No validation results or artifacts available. Please complete validation phase first."
    
    # Call the gate function
    try:
        gate_result = deployment_approval_gate(runtime.state)
        
        # Update runtime state with gate result
        if "human_approval_status" in gate_result:
            if "human_approval_status" not in runtime.state:
                runtime.state["human_approval_status"] = {}
            runtime.state["human_approval_status"].update(gate_result["human_approval_status"])
        
        if "messages" in gate_result:
            if "messages" not in runtime.state:
                runtime.state["messages"] = []
            runtime.state["messages"].extend(gate_result["messages"])
        
        # Extract approval status
        approval = gate_result.get("human_approval_status", {}).get("deployment")
        if approval:
            status = approval.status if hasattr(approval, "status") else approval.get("status", "pending")
            reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer")
            
            if status == "approved":
                return f"✅ Deployment approved by {reviewer or 'reviewer'}. Workflow complete! Helm chart is ready for deployment."
            elif status == "rejected":
                return f"❌ Deployment rejected by {reviewer or 'reviewer'}. Workflow ended without deployment."
            else:
                return f"⏳ Deployment approval pending. Waiting for human approval..."
        
        return "⏳ Deployment approval initiated. Waiting for human approval..."
        
    except Exception as e:
        gate_tools_logger.log_structured(
            level="ERROR",
            message=f"Error in deployment approval gate: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        return f"❌ Error requesting deployment approval: {str(e)}"


def create_hitl_gate_tools() -> List:
    """
    Create list of HITL gate tools for supervisor integration.
    
    Returns:
        List of gate tool functions
    """
    tools = [
        request_planning_review,
        request_security_review,
        request_deployment_approval
    ]
    
    gate_tools_logger.log_structured(
        level="INFO",
        message="Created HITL gate tools",
        extra={
            "tool_count": len(tools),
            "tool_names": [t.name for t in tools]
        }
    )
    
    return tools

