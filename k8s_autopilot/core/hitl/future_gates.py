from typing import Dict, Any, List
from k8s_autopilot.core.state.base import MainSupervisorState, ValidationResult
from k8s_autopilot.core.hitl.utils import is_approved, format_review_data, update_approval_status
from langchain_core.messages import AIMessage
from langgraph.types import interrupt

def deployment_approval_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    HITL gate for deployment approval.
    
    Requires final human confirmation before deploying verified artifacts.
    This gate provides a comprehensive summary of validation results and
    prepared artifacts.
    
    Args:
        state: Current MainSupervisorState
        
    Returns:
        Updated state with approval status, deployment readiness, and messages
    """
    # Check if already approved
    if is_approved(state, "deployment"):
        approval = state.get("human_approval_status", {}).get("deployment")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return {
            "messages": [
                AIMessage(
                    content=f"Deployment already approved by {reviewer or 'previous reviewer'}"
                )
            ]
        }
    
    # Extract validation summary
    validation_results = state.get("validation_results", [])
    passed_checks = [r for r in validation_results if (isinstance(r, dict) and r.get("passed")) or (hasattr(r, "passed") and r.passed)]
    failed_checks = [r for r in validation_results if (isinstance(r, dict) and not r.get("passed")) or (hasattr(r, "passed") and not r.passed)]
    
    # Extract generated artifacts summary
    generated_artifacts = state.get("generated_artifacts", {})
    artifact_summary = "\n".join([f"- {k}: {v[:50]}..." for k, v in generated_artifacts.items()])
    
    # Chart name
    chart_name = "Application"
    planning_output = state.get("planning_output", {})
    if planning_output:
        if isinstance(planning_output, dict):
            reqs = planning_output.get("parsed_requirements", {})
            chart_name = reqs.get("app_name", chart_name)
    
    summary = f"""# 🚀 Deployment Approval Required

## Validation Status
- ✅ Passed Checks: {len(passed_checks)}
- ❌ Failed Checks: {len(failed_checks)}
- 📋 Total Checks: {len(validation_results)}

## Artifacts Ready for Deployment
{artifact_summary}

## Deployment Target
- Cluster: (Configured in kubeconfig)
- Namespace: (From chart values)

**Do you approve the deployment of `{chart_name}` to the cluster?** (approve/reject)"""

    # Build review data
    review_data = format_review_data(
        phase="deployment",
        summary=summary,
        data={
            "validation_summary": {
                "passed": len(passed_checks),
                "failed": len(failed_checks),
                "total": len(validation_results)
            },
            "artifacts": list(generated_artifacts.keys()),
            "chart_name": chart_name,
            "review_type": "deployment_approval"
        },
        required_action="approve",
        options=["approve", "reject"]
    )
    
    # Trigger interrupt
    human_decision = interrupt(review_data)
    
    # Process human decision
    decision = human_decision.get("decision", "rejected")
    reviewer = human_decision.get("reviewer")
    comments = human_decision.get("comments")
    
    updated_approvals = update_approval_status(
        state=state,
        approval_type="deployment", # type: ignore
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    # Determine deployment readiness
    is_ready = (decision == "approved")
    
    return {
        "human_approval_status": updated_approvals,
        "deployment_ready": is_ready,
        "messages": [
            AIMessage(
                content=f"Deployment approval: {decision} by {reviewer or 'reviewer'}"
                + (f": {comments}" if comments else "")
            )
        ]
    }
