"""
Client helper functions for HITL interrupt handling and resume.

These utilities help clients interact with HITL interrupts and resume workflows.
"""

from typing import Dict, Any, Optional
from langgraph.types import Command
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for client helpers
client_logger = AgentLogger("k8sAutopilotHITLClient")


def create_resume_command(
    decision: str,
    reviewer: Optional[str] = None,
    comments: Optional[str] = None
) -> Command:
    """
    Create a Command for resuming from HITL interrupt.
    
    Args:
        decision: Human decision ("approved", "rejected", "modified")
        reviewer: Optional reviewer identifier (e.g., email)
        comments: Optional review comments
        
    Returns:
        Command object for resuming workflow
        
    Example:
        resume_cmd = create_resume_command(
            decision="approved",
            reviewer="john.doe@example.com",
            comments="LGTM"
        )
        result = await agent.stream(resume_cmd, context_id, task_id)
    """
    resume_data = {
        "decision": decision,
        "reviewer": reviewer,
        "comments": comments
    }
    
    client_logger.log_structured(
        level="INFO",
        message="Created resume command",
        extra={
            "decision": decision,
            "has_reviewer": reviewer is not None,
            "has_comments": comments is not None
        }
    )
    
    return Command(resume=resume_data)


def extract_interrupt_data(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract HITL interrupt data from AgentResponse.
    
    Args:
        response: AgentResponse dict or object
        
    Returns:
        Interrupt data dictionary or None if not an interrupt
    """
    # Handle both dict and object
    if hasattr(response, 'content'):
        content = response.content
        metadata = response.metadata
    else:
        content = response.get('content', {})
        metadata = response.get('metadata', {})
    
    # Check if this is a HITL interrupt
    if isinstance(content, dict) and content.get('type') == 'hitl_interrupt':
        return {
            'phase': content.get('phase'),
            'summary': content.get('summary'),
            'required_action': content.get('required_action'),
            'options': content.get('options'),
            'data': content.get('data', {}),
            'metadata': metadata
        }
    
    # Check metadata for interrupt type
    if metadata.get('interrupt_type') == 'hitl_gate':
        return metadata.get('interrupt_data', {})
    
    return None


def format_interrupt_for_display(interrupt_data: Dict[str, Any]) -> str:
    """
    Format interrupt data for human-readable display.
    
    Args:
        interrupt_data: Interrupt data dictionary
        
    Returns:
        Formatted string for display
    """
    phase = interrupt_data.get('phase', 'unknown')
    summary = interrupt_data.get('summary', 'Human review required')
    options = interrupt_data.get('options', [])
    
    separator = '=' * 60
    formatted = f"""
{separator}
HITL INTERRUPT: {phase.upper()}
{separator}

{summary}

Available options: {', '.join(options)}

Please provide your decision to continue.
"""
    
    return formatted.strip()


def validate_resume_decision(
    decision: str,
    allowed_options: Optional[list] = None
) -> bool:
    """
    Validate that a resume decision is valid.
    
    Args:
        decision: Decision string
        allowed_options: Optional list of allowed decisions
        
    Returns:
        True if valid, False otherwise
    """
    if allowed_options is None:
        allowed_options = ["approved", "rejected", "modified"]
    
    return decision.lower() in [opt.lower() for opt in allowed_options]

