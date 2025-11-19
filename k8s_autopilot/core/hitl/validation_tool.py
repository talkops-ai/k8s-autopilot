"""
Request validation tool for supervisor.

This tool can be called by the supervisor to validate if a request
is related to Helm chart generation/deployment.
"""

from langchain.tools import tool, ToolRuntime
from langgraph.types import interrupt
from k8s_autopilot.core.state.base import MainSupervisorState
from k8s_autopilot.core.hitl.request_validator import (
    validate_and_reject_non_helm,
    is_helm_related_request
)
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for validation tool
validation_tool_logger = AgentLogger("k8sAutopilotValidationTool")


@tool
async def validate_request_scope(
    user_query: str,
    runtime: ToolRuntime[None, MainSupervisorState]
) -> str:
    """
    Validate if a user request is within scope (Helm chart generation/deployment).
    
    Use this tool when:
    - You receive a user request and need to verify it's Helm/Kubernetes related
    - The request seems ambiguous or unclear
    - You want to confirm before proceeding with workflow
    
    This tool will:
    1. Check if request is clearly Helm-related → return "valid"
    2. Check if request is clearly NOT Helm-related → return rejection message
    3. If ambiguous → trigger interrupt for human confirmation
    
    Args:
        user_query: The user's request to validate
        
    Returns:
        "valid" if request is Helm-related, or rejection message if not
    """
    validation_tool_logger.log_structured(
        level="INFO",
        message="Validating request scope",
        extra={"query_length": len(user_query), "query_preview": user_query[:100]}
    )
    
    is_valid, rejection_message, interrupt_data = validate_and_reject_non_helm(
        user_query,
        require_confirmation=True  # Ambiguous requests trigger interrupt
    )
    
    if is_valid:
        return "✅ Request is valid - related to Helm chart generation/deployment. Proceed with workflow."
    
    # If ambiguous, trigger interrupt for human confirmation
    if interrupt_data:
        validation_tool_logger.log_structured(
            level="INFO",
            message="Request is ambiguous - triggering interrupt for confirmation",
            extra={"query_preview": user_query[:100]}
        )
        
        # Trigger interrupt
        human_response = interrupt(interrupt_data)
        
        decision = human_response.get("decision", "reject")
        
        if decision == "approve":
            return "✅ Request confirmed as Helm-related by human reviewer. Proceed with workflow."
        else:
            return f"❌ Request rejected by human reviewer. {rejection_message or 'Request is not related to Helm chart generation/deployment.'}"
    
    # Direct rejection
    return f"❌ {rejection_message}"

