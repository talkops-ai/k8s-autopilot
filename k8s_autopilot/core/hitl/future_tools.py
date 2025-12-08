from typing import List
from langchain.tools import tool, ToolRuntime
from k8s_autopilot.core.state.base import MainSupervisorState
from k8s_autopilot.core.hitl.utils import is_approved
from k8s_autopilot.utils.logger import AgentLogger

# Import gate from future_gates but don't expose it in __init__
from k8s_autopilot.core.hitl.future_gates import deployment_approval_gate

gate_tools_logger = AgentLogger("k8sAutopilotFutureGates")

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
            # If approved, we effectively complete the workflow here for this release
            if status == "approved":
                # Manual update to workflow state as this is the final step
                if "workflow_state" in runtime.state:
                    # Mark deployment complete
                    ws = runtime.state["workflow_state"]
                    # If it's an object use methods, if dict update keys
                    if hasattr(ws, "set_phase_complete"):
                        ws.set_phase_complete("deployment")
                        ws.set_approval("deployment", True)
                    elif isinstance(ws, dict):
                        ws["deployment_complete"] = True
                        ws["deployment_approved"] = True
                        ws["current_phase"] = "complete"

                return f"✅ Deployment approved. Workflow complete! Helm chart is ready."
            elif status == "rejected":
                return f"❌ Deployment rejected. Workflow ended."
            else:
                return f"⏳ Deployment approval pending..."
        
        return "⏳ Deployment approval initiated..."
        
    except Exception as e:
        gate_tools_logger.log_structured(
            level="ERROR",
            message=f"Error in deployment approval gate: {e}",
            extra={"error": str(e)}
        )
        return f"❌ Error requesting deployment approval: {str(e)}"
