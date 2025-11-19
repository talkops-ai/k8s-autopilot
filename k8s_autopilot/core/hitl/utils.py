"""
Utility functions for HITL approval management and interrupt handling.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from k8s_autopilot.core.state.base import ApprovalStatus, MainSupervisorState


def create_approval_status(
    status: str = "pending",
    reviewer: Optional[str] = None,
    comments: Optional[str] = None
) -> ApprovalStatus:
    """
    Create an ApprovalStatus instance.
    
    Args:
        status: Approval status ("pending", "approved", "rejected", "modified")
        reviewer: Optional reviewer identifier (e.g., email)
        comments: Optional review comments
        
    Returns:
        ApprovalStatus instance
    """
    return ApprovalStatus(
        status=status,
        reviewer=reviewer,
        comments=comments,
        timestamp=datetime.now(timezone.utc)
    )


def update_approval_status(
    state: Dict[str, Any],
    approval_type: str,
    decision: str,
    reviewer: Optional[str] = None,
    comments: Optional[str] = None
) -> Dict[str, ApprovalStatus]:
    """
    Update approval status in state.
    
    Args:
        state: Current state dictionary
        approval_type: Type of approval ("planning", "security", "deployment")
        decision: Decision made ("approved", "rejected", "modified")
        reviewer: Optional reviewer identifier
        comments: Optional review comments
        
    Returns:
        Updated human_approval_status dictionary
    """
    current_approvals = state.get("human_approval_status", {})
    
    approval_status = create_approval_status(
        status=decision,
        reviewer=reviewer,
        comments=comments
    )
    
    updated_approvals = {
        **current_approvals,
        approval_type: approval_status
    }
    
    return updated_approvals


def format_review_data(
    phase: str,
    summary: str,
    data: Optional[Dict[str, Any]] = None,
    required_action: str = "approve",
    options: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Format review data for interrupt payload.
    
    Args:
        phase: Workflow phase ("planning", "security", "deployment")
        summary: Human-readable summary of what needs review
        data: Optional additional data to include
        required_action: Required action from human
        options: Available decision options
        
    Returns:
        Formatted review data dictionary
    """
    if options is None:
        options = ["approve", "reject"]
    
    review_data = {
        "phase": phase,
        "summary": summary,
        "required_action": required_action,
        "options": options,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    if data:
        review_data.update(data)
    
    return review_data


def build_interrupt_payload(
    phase: str,
    summary: str,
    chart_info: Optional[Dict[str, Any]] = None,
    validation_summary: Optional[Dict[str, Any]] = None,
    security_issues: Optional[List[Dict[str, Any]]] = None,
    options: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Build a comprehensive interrupt payload for HITL gates.
    
    Args:
        phase: Workflow phase
        summary: Review summary
        chart_info: Optional chart information
        validation_summary: Optional validation results summary
        security_issues: Optional list of security issues
        options: Available decision options
        
    Returns:
        Complete interrupt payload dictionary
    """
    data = {}
    
    if chart_info:
        data["chart_info"] = chart_info
    
    if validation_summary:
        data["validation_summary"] = validation_summary
    
    if security_issues:
        data["security_issues"] = security_issues
    
    return format_review_data(
        phase=phase,
        summary=summary,
        data=data,
        options=options
    )


def extract_planning_summary(plan: Dict[str, Any]) -> str:
    """
    Extract and format planning summary from planning output.
    
    Args:
        plan: Planning output dictionary
        
    Returns:
        Formatted summary string
    """
    chart_name = plan.get("chart_name", "N/A")
    chart_version = plan.get("chart_version", "N/A")
    description = plan.get("description", "N/A")
    resources = plan.get("resources_to_create", [])
    security_policies = plan.get("security_policies", [])
    dependencies = plan.get("chart_dependencies", [])
    
    summary = f"""# Planning Review Required

## Chart Information
- **Name**: {chart_name}
- **Version**: {chart_version}
- **Description**: {description}

## Resources to Create
{chr(10).join(f"- {r}" for r in resources) if resources else "- None specified"}

## Security Policies
{chr(10).join(f"- {p}" for p in security_policies) if security_policies else "- None specified"}

## Dependencies
{len(dependencies)} chart dependencies

Please review and approve/reject/modify the plan."""
    
    return summary


def extract_security_summary(security_issues: List[Dict[str, Any]]) -> str:
    """
    Extract and format security review summary.
    
    Args:
        security_issues: List of security issue dictionaries
        
    Returns:
        Formatted summary string
    """
    critical_count = len([i for i in security_issues if i.get("severity") == "critical"])
    error_count = len([i for i in security_issues if i.get("severity") == "error"])
    
    issues_text = "\n".join(
        f"### {i.get('severity', 'unknown').upper()}: {i.get('message', 'N/A')}"
        for i in security_issues[:5]
    )
    
    summary = f"""# Security Review Required

## Critical Issues Found: {critical_count}
## Error Issues Found: {error_count}

{issues_text if issues_text else "No critical issues found."}

Please review security findings and approve deployment."""
    
    return summary


def extract_deployment_summary(
    validation_summary: Dict[str, Any],
    chart_artifacts: Optional[List[str]] = None
) -> str:
    """
    Extract and format deployment approval summary.
    
    Args:
        validation_summary: Validation results summary
        chart_artifacts: Optional list of generated artifact names
        
    Returns:
        Formatted summary string
    """
    total = validation_summary.get("total_checks", 0)
    passed = validation_summary.get("passed", 0)
    failed = validation_summary.get("failed", 0)
    
    artifacts_text = ""
    if chart_artifacts:
        artifacts_text = f"\n## Generated Artifacts\n{chr(10).join(f"- {a}" for a in chart_artifacts)}\n"
    
    summary = f"""# Final Deployment Approval

All validations complete. Chart ready for deployment to cluster.
{artifacts_text}
## Validation Summary
- Total checks: {total}
- Passed: {passed}
- Failed: {failed}
- Chart structure: {'✓ Valid' if failed == 0 else '✗ Issues found'}
- Security scan: {'✓ Passed' if failed == 0 else '✗ Issues found'}
- Best practices: {'✓ Compliant' if failed == 0 else '✗ Issues found'}

Approve to deploy the Helm chart?"""
    
    return summary


def is_approved(
    state: Dict[str, Any],
    approval_type: str
) -> bool:
    """
    Check if a specific approval type is already approved.
    
    Args:
        state: Current state dictionary
        approval_type: Type of approval to check
        
    Returns:
        True if approved, False otherwise
    """
    approvals = state.get("human_approval_status", {})
    approval = approvals.get(approval_type)
    
    if not approval:
        return False
    
    # Handle both ApprovalStatus objects and dictionaries
    if isinstance(approval, ApprovalStatus):
        return approval.status in ["approved", "modified"]
    elif isinstance(approval, dict):
        return approval.get("status") in ["approved", "modified"]
    
    return False

