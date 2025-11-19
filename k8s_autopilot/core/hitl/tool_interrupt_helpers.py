"""
Helper functions for tools to trigger HITL interrupts.

These utilities make it easy for tools within swarms to request
human input or approval during execution.
"""

from typing import Dict, Any, Optional, List
from langgraph.types import interrupt
from k8s_autopilot.core.hitl.utils import format_review_data
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for tool interrupt helpers
tool_interrupt_logger = AgentLogger("k8sAutopilotToolInterrupt")


def request_tool_clarification(
    tool_name: str,
    phase: str,
    summary: str,
    ambiguous_data: Dict[str, Any],
    options: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Helper function for tools to request human clarification.
    
    Use this when a tool encounters ambiguous data and needs
    human input to proceed.
    
    Args:
        tool_name: Name of the tool requesting clarification
        phase: Current workflow phase (e.g., "planning", "generation")
        summary: Human-readable summary of what needs clarification
        ambiguous_data: Dictionary of ambiguous fields and their values
        options: Available decision options (default: ["approve", "clarify", "reject"])
        
    Returns:
        Human response dictionary with:
        - decision: User's decision
        - clarification: Clarified data (if decision == "clarify")
        - reviewer: Optional reviewer identifier
        - comments: Optional comments
        
    Example:
        human_response = request_tool_clarification(
            tool_name="parse_requirements",
            phase="planning",
            summary="Database type is ambiguous",
            ambiguous_data={"database_type": "unknown"}
        )
        
        if human_response["decision"] == "clarify":
            database_type = human_response["clarification"]["database_type"]
    """
    if options is None:
        options = ["approve", "clarify", "reject"]
    
    interrupt_data = format_review_data(
        phase=phase,
        summary=summary,
        data={
            "type": "tool_interrupt",
            "tool_name": tool_name,
            "ambiguous_data": ambiguous_data
        },
        required_action="clarify",
        options=options
    )
    
    tool_interrupt_logger.log_structured(
        level="INFO",
        message=f"Tool {tool_name} requesting clarification",
        extra={
            "tool_name": tool_name,
            "phase": phase,
            "ambiguous_fields": list(ambiguous_data.keys())
        }
    )
    
    # Trigger interrupt - execution pauses here
    human_response = interrupt(interrupt_data)
    
    return human_response


def request_tool_approval(
    tool_name: str,
    phase: str,
    summary: str,
    data_to_review: Dict[str, Any],
    options: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Helper function for tools to request human approval.
    
    Use this when a tool produces output that needs human
    review and approval before proceeding.
    
    Args:
        tool_name: Name of the tool requesting approval
        phase: Current workflow phase
        summary: Human-readable summary of what needs approval
        data_to_review: Dictionary of data that needs review
        options: Available decision options (default: ["approve", "reject"])
        
    Returns:
        Human response dictionary with:
        - decision: User's decision ("approved" or "rejected")
        - modifications: Optional modifications (if decision == "modify")
        - reviewer: Optional reviewer identifier
        - comments: Optional comments
        
    Example:
        human_response = request_tool_approval(
            tool_name="design_kubernetes_architecture",
            phase="planning",
            summary="Complex architecture design ready for review",
            data_to_review={"architecture": architecture_dict}
        )
        
        if human_response["decision"] == "approved":
            # Proceed with architecture
            pass
    """
    if options is None:
        options = ["approve", "reject"]
    
    interrupt_data = format_review_data(
        phase=phase,
        summary=summary,
        data={
            "type": "tool_interrupt",
            "tool_name": tool_name,
            "data_to_review": data_to_review
        },
        required_action="approve",
        options=options
    )
    
    tool_interrupt_logger.log_structured(
        level="INFO",
        message=f"Tool {tool_name} requesting approval",
        extra={
            "tool_name": tool_name,
            "phase": phase,
            "data_keys": list(data_to_review.keys())
        }
    )
    
    # Trigger interrupt - execution pauses here
    human_response = interrupt(interrupt_data)
    
    return human_response


def request_tool_input(
    tool_name: str,
    phase: str,
    question: str,
    input_type: str = "text",
    validation: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Helper function for tools to request human input.
    
    Use this when a tool needs specific information from a human
    that it cannot determine automatically.
    
    Args:
        tool_name: Name of the tool requesting input
        phase: Current workflow phase
        question: Question to ask the human
        input_type: Type of input expected ("text", "number", "choice", etc.)
        validation: Optional validation rules for the input
        
    Returns:
        Human response dictionary with:
        - input: The provided input value
        - reviewer: Optional reviewer identifier
        - comments: Optional comments
        
    Example:
        human_response = request_tool_input(
            tool_name="estimate_resources",
            phase="planning",
            question="What is the expected traffic volume?",
            input_type="number",
            validation={"min": 0, "max": 1000000}
        )
        
        traffic_volume = human_response["input"]
    """
    summary = f"""
# Input Required

{question}

Input type: {input_type}
{f"Validation: {validation}" if validation else ""}
    """
    
    interrupt_data = format_review_data(
        phase=phase,
        summary=summary,
        data={
            "type": "tool_interrupt",
            "tool_name": tool_name,
            "input_type": input_type,
            "validation": validation or {}
        },
        required_action="input",
        options=["submit", "cancel"]
    )
    
    tool_interrupt_logger.log_structured(
        level="INFO",
        message=f"Tool {tool_name} requesting input",
        extra={
            "tool_name": tool_name,
            "phase": phase,
            "input_type": input_type
        }
    )
    
    # Trigger interrupt - execution pauses here
    human_response = interrupt(interrupt_data)
    
    return human_response

