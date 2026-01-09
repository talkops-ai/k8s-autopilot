"""
Custom interrupt gate functions for HITL.

These gates are used for phase-level approvals in the workflow.
Each gate:
1. Checks if already approved (skip if yes)
2. Prepares review data
3. Triggers interrupt for human review
4. Processes human decision and updates approval status
"""

from typing import Dict, Any, List
from datetime import datetime, timezone
import json
from langgraph.types import interrupt
from langchain_core.messages import AIMessage

from k8s_autopilot.core.state.base import (
    MainSupervisorState,
    ApprovalStatus,
    ChartPlan,
    ValidationResult
)
from k8s_autopilot.core.hitl.utils import (
    is_approved,
    update_approval_status,
    extract_planning_summary,
    extract_security_summary,
    extract_deployment_summary,
    build_interrupt_payload,
    format_review_data
)



def generic_review_gate(
    state: MainSupervisorState, 
    phase: str, 
    review_title: str,
    review_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Generic HITL gate for any phase.
    
    Args:
        state: Current MainSupervisorState
        phase: The phase name (e.g., "planning", "security")
        review_title: Title for the review UI
        review_data: Structured data to display
        
    Returns:
        Updated state with approval status and messages
    """
    # Check if already approved
    if is_approved(state, phase):
        approval = state.get("human_approval_status", {}).get(phase)
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return {
            "messages": [
                AIMessage(
                    content=f"{phase.capitalize()} already approved by {reviewer or 'previous reviewer'}."
                )
            ]
        }
    
    # Build review data structure
    formatted_data = format_review_data(
        phase=phase,
        summary=review_title,
        data=review_data,
        required_action="approve",
        options=["approve", "reject", "modify"]
    )
    
    # Trigger interrupt
    human_decision = interrupt(formatted_data)
    
    # Handle resume value: interrupt() may return a string when resuming
    if isinstance(human_decision, str):
        # Try to parse as JSON if it looks like JSON
        try:
            # Check if string contains JSON context
            if "CONTEXT:" in human_decision:
                # Extract JSON part after "CONTEXT:"
                json_part = human_decision.split("CONTEXT:")[-1].strip()
                human_decision = json.loads(json_part)
            else:
                # Try parsing the whole string as JSON
                human_decision = json.loads(human_decision)
        except (json.JSONDecodeError, ValueError):
            # If not JSON, treat as simple approval decision
            decision_lower = human_decision.lower().strip()
            if "approve" in decision_lower or decision_lower == "approved":
                human_decision = {"decision": "approved", "comments": human_decision}
            else:
                human_decision = {"decision": "rejected", "comments": human_decision}
    
    # Ensure human_decision is a dict
    if not isinstance(human_decision, dict):
        human_decision = {"decision": "rejected", "comments": str(human_decision)}
    
    # Process decision
    decision = human_decision.get("decision", "rejected")
    reviewer = human_decision.get("reviewer")
    comments = human_decision.get("comments")
    
    updated_approvals = update_approval_status(
        state=state,
        approval_type=phase, # type: ignore
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    return {
        "human_approval_status": updated_approvals,
        "messages": [
            AIMessage(
                content=f"{phase.capitalize()} review: {decision} by {reviewer or 'reviewer'}"
                + (f": {comments}" if comments else "")
            )
        ]
    }

def planning_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    Wrapper around generic_review_gate for backward compatibility.
    Allows specialized logic for extracting planning data before calling generic gate.
    """
    # Get planning output
    planning_output = state.get("planning_output")
    
    if not planning_output:
        return {
            "messages": [AIMessage(content="No planning output available.")]
        }

    # Convert to dict
    if isinstance(planning_output, ChartPlan):
        plan_dict = planning_output.model_dump()
    elif isinstance(planning_output, dict):
        plan_dict = planning_output
    else:
        try:
             plan_dict = planning_output.model_dump()
        except:
             plan_dict = dict(planning_output)
    
    # Extract summary
    summary = extract_planning_summary(plan_dict)
    
    return generic_review_gate(
        state=state,
        phase="planning",
        review_title=summary,
        review_data={
            "chart_plan": plan_dict,
            "chart_info": {
                "name": plan_dict.get("chart_name", "N/A"),
                "version": plan_dict.get("chart_version", "N/A")
            }
        }
    )



def generation_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    HITL gate for generation review (Artifacts & Workspace).
    
    Reviews generated artifacts and requests workspace directory for chart validation.
    
    Args:
        state: Current MainSupervisorState
        
    Returns:
        Updated state with approval status, workspace_dir, and messages
    """
    # Check if already approved
    if is_approved(state, "generation"):
        approval = state.get("human_approval_status", {}).get("generation")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return {
            "messages": [
                AIMessage(
                    content=f"Generation already approved by {reviewer or 'previous reviewer'}"
                )
            ]
        }
    
    # Extract chart/application name
    chart_name = "the application"
    helm_chart_artifacts = state.get("helm_chart_artifacts", {})
    if helm_chart_artifacts and "Chart.yaml" in helm_chart_artifacts:
        try:
            import yaml
            chart_metadata = yaml.safe_load(helm_chart_artifacts["Chart.yaml"])
            chart_name = chart_metadata.get("name", chart_name)
        except Exception:
            pass
            
    # Prepare artifact list
    chart_files = list(helm_chart_artifacts.keys())
    if not chart_files:
        chart_files_text = "(Helm chart artifacts have been generated - exact file list not available in state)"
    else:
        chart_files_text = "\n".join(f"- {f}" for f in chart_files[:10])
        if len(chart_files) > 10:
            chart_files_text += f"\n- ... and {len(chart_files) - 10} more files"
            
    # Construct the exact summary requested by user
    full_summary = f"""# ✅ Template Generation Complete - Ready for Validation
    
    ## Generated Helm Chart Artifacts for `{chart_name}`
    
    The following Helm chart files have been generated by the Template Agent:
    
    {chart_files_text}
    
    ---
    
    ## 📁 Workspace Directory Configuration
    
    **Please specify the workspace directory where the Helm chart should be written:**
    
    | Option | Description |
    |--------|-------------|
    | Default | `/tmp/helm-charts` |
    | Custom | Provide your own path (e.g., `/home/user/charts/{chart_name}`) |
    
    ⚠️ **Note:** If you are running this in a Docker container, make sure the directory you specify is mounted to your local disk.
    
    **To specify a workspace directory:** 
    - Reply "approve" to use the default directory.
    - Reply "approve /your/custom/path" to use a custom directory.
    
    ---
    
    ## 🔍 What Happens Next (After Your Approval)
    
    **Important:** Validation has NOT started yet. The workflow will proceed as follows:
    
    1. **Write Files**: The system will write the generated files to your chosen workspace directory.
    2. **Run Validation**: After files are written, the Validator Agent will automatically run:
       - **Helm lint** (Syntax check)
       - **Helm template** (Rendering check)
       - **Helm dry-run** (Cluster compatibility check, if available)
    3. **Final Report**: You will receive a comprehensive validation report with results from all checks.
    """

    # Build review data
    review_data = format_review_data(
        phase="generation",
        summary=full_summary,
        data={
            "chart_name": chart_name,
            "chart_files": list(helm_chart_artifacts.keys()),
            "file_count": len(helm_chart_artifacts),
            "workspace_dir_prompt": True,
            "review_type": "artifact_review"
        },
        required_action="approve",
        options=["approve", "reject"]
    )
    
    # Trigger interrupt
    human_decision = interrupt(review_data)
    
    # Handle resume value: interrupt() may return a string when resuming
    if isinstance(human_decision, str):
        # Try to parse as JSON if it looks like JSON
        try:
            # Check if string contains JSON context
            if "CONTEXT:" in human_decision:
                # Extract JSON part after "CONTEXT:"
                json_part = human_decision.split("CONTEXT:")[-1].strip()
                human_decision = json.loads(json_part)
            else:
                # Try parsing the whole string as JSON
                human_decision = json.loads(human_decision)
        except (json.JSONDecodeError, ValueError):
            # If not JSON, treat as simple approval decision
            decision_lower = human_decision.lower().strip()
            if "approve" in decision_lower or decision_lower == "approved":
                human_decision = {"decision": "approved", "comments": human_decision}
            else:
                human_decision = {"decision": "rejected", "comments": human_decision}
    
    # Ensure human_decision is a dict
    if not isinstance(human_decision, dict):
        human_decision = {"decision": "rejected", "comments": str(human_decision)}
    
    # Process human decision
    decision = human_decision.get("decision", "rejected")
    reviewer = human_decision.get("reviewer")
    comments = human_decision.get("comments")
    
    # Extract workspace_dir
    # Extract workspace_dir from structured response if available
    # For unstructured text (comments), we rely on the Supervisor Agent to parse the path
    workspace_dir = human_decision.get("workspace_dir", "/tmp/helm-charts")
    
    updated_approvals = update_approval_status(
        state=state,
        approval_type="generation", # type: ignore
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    return {
        "human_approval_status": updated_approvals,
        "workspace_dir": workspace_dir,
        "messages": [
            AIMessage(
                content=f"Generation review: {decision} by {reviewer or 'reviewer'}"
                + (f": {comments}" if comments else "")
                + f" | Workspace directory: {workspace_dir}"
            )
        ]
    }




