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


def planning_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    Custom HITL gate for planning review.
    
    Uses interrupt() function for dynamic, conditional interrupts.
    Checks if planning is already approved, and if not, triggers an interrupt
    for human review of the chart plan.
    
    Args:
        state: Current MainSupervisorState
        
    Returns:
        Updated state with approval status and messages
    """
    # Check if already approved
    if is_approved(state, "planning"):
        approval = state.get("human_approval_status", {}).get("planning")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return {
            "messages": [
                AIMessage(
                    content=f"Planning already approved by {reviewer or 'previous reviewer'}, proceeding to generation..."
                )
            ]
        }
    
    # Get planning output - handle both dict and ChartPlan object
    planning_output = state.get("planning_output")
    
    if not planning_output:
        return {
            "messages": [
                AIMessage(
                    content="No planning output available for review. Skipping planning review gate."
                )
            ]
        }
    
    # Convert to dict if it's a ChartPlan object
    if isinstance(planning_output, ChartPlan):
        plan_dict = planning_output.model_dump()
    elif isinstance(planning_output, dict):
        plan_dict = planning_output
    else:
        # Try to convert if it has model_dump method
        try:
            plan_dict = planning_output.model_dump() if hasattr(planning_output, "model_dump") else dict(planning_output)
        except Exception:
            plan_dict = {"error": "Could not parse planning output"}
    
    # Extract summary using utility function
    summary = extract_planning_summary(plan_dict)
    
    # Build review data
    review_data = format_review_data(
        phase="planning",
        summary=summary,
        data={
            "chart_plan": plan_dict,
            "chart_info": {
                "name": plan_dict.get("chart_name", "N/A"),
                "version": plan_dict.get("chart_version", "N/A"),
                "description": plan_dict.get("description", "N/A")
            }
        },
        required_action="approve",
        options=["approve", "reject", "modify"]
    )
    
    # Trigger interrupt - execution pauses here
    # Returns control to caller with review_data in __interrupt__ field
    # After resume, human_decision contains the response
    human_decision = interrupt(review_data)
    
    # Process human decision and update approval status
    decision = human_decision.get("decision", "rejected")
    reviewer = human_decision.get("reviewer")
    comments = human_decision.get("comments")
    
    updated_approvals = update_approval_status(
        state=state,
        approval_type="planning",
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    return {
        "human_approval_status": updated_approvals,
        "messages": [
            AIMessage(
                content=f"Planning {decision} by {reviewer or 'reviewer'}"
                + (f": {comments}" if comments else "")
            )
        ]
    }


def security_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    HITL gate for security review.
    
    Reviews security scan results and requires human approval before proceeding
    to validation/deployment if critical or error-level issues are found.
    
    Args:
        state: Current MainSupervisorState
        
    Returns:
        Updated state with approval status and messages
    """
    # Check if already approved
    if is_approved(state, "security"):
        approval = state.get("human_approval_status", {}).get("security")
        reviewer = approval.reviewer if hasattr(approval, "reviewer") else approval.get("reviewer") if isinstance(approval, dict) else None
        return {
            "messages": [
                AIMessage(
                    content=f"Security already approved by {reviewer or 'previous reviewer'}"
                )
            ]
        }
    
    # Get security scan results from validation_results
    validation_results = state.get("validation_results", [])
    
    # Filter security issues (critical and error severity)
    security_issues: List[Dict[str, Any]] = []
    for result in validation_results:
        # Handle both ValidationResult objects and dicts
        if isinstance(result, ValidationResult):
            if result.validator == "security_scanner" and result.severity in ["error", "critical"]:
                security_issues.append({
                    "severity": result.severity,
                    "message": result.message,
                    "details": result.details,
                    "timestamp": result.timestamp.isoformat() if hasattr(result.timestamp, "isoformat") else str(result.timestamp)
                })
        elif isinstance(result, dict):
            if result.get("validator") == "security_scanner" and result.get("severity") in ["error", "critical"]:
                security_issues.append(result)
    
    # Extract summary using utility function
    summary = extract_security_summary(security_issues)
    
    # Build review data
    review_data = format_review_data(
        phase="security",
        summary=summary,
        data={
            "security_issues": security_issues,
            "issue_count": {
                "critical": len([i for i in security_issues if i.get("severity") == "critical"]),
                "error": len([i for i in security_issues if i.get("severity") == "error"]),
                "total": len(security_issues)
            }
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
        approval_type="security",
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    return {
        "human_approval_status": updated_approvals,
        "messages": [
            AIMessage(
                content=f"Security review: {decision} by {reviewer or 'reviewer'}"
                + (f": {comments}" if comments else "")
            )
        ]
    }


def deployment_approval_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    HITL gate for final deployment approval.
    
    Final checkpoint before deploying to cluster. Reviews all validations,
    generated artifacts, and requires explicit human approval.
    
    Args:
        state: Current MainSupervisorState
        
    Returns:
        Updated state with approval status and messages
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
    
    # Get validation results
    validation_results = state.get("validation_results", [])
    
    # Calculate validation summary
    total_checks = len(validation_results)
    passed = sum(
        1 for v in validation_results
        if (v.passed if isinstance(v, ValidationResult) else v.get("passed", False))
    )
    failed = total_checks - passed
    
    validation_summary = {
        "total_checks": total_checks,
        "passed": passed,
        "failed": failed
    }
    
    # Get generated artifacts
    generated_artifacts = state.get("generated_artifacts", {})
    chart_artifacts = list(generated_artifacts.keys()) if generated_artifacts else []
    
    # Extract summary using utility function
    summary = extract_deployment_summary(
        validation_summary=validation_summary,
        chart_artifacts=chart_artifacts
    )
    
    # Build review data
    review_data = format_review_data(
        phase="deployment",
        summary=summary,
        data={
            "chart_artifacts": chart_artifacts,
            "validation_summary": validation_summary,
            "artifact_count": len(chart_artifacts)
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
        approval_type="deployment",
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    return {
        "human_approval_status": updated_approvals,
        "messages": [
            AIMessage(
                content=f"Deployment: {decision} by {reviewer or 'reviewer'}"
                + (f": {comments}" if comments else "")
            )
        ]
    }
