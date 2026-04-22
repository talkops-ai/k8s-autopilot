"""
Shared HITL tool factory for Helm planner sub-agents.

Creates a ``request_human_input`` tool for any planning sub-agent that needs
to pause execution and ask the user for clarification.

Reference: aws-orchestrator tf_planner/new_module/shared/hitl.py
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from k8s_autopilot.core.state.helm_planner_state import HelmPlannerState
from k8s_autopilot.utils.logger import AgentLogger
from pydantic import BaseModel, Field


def create_hitl_tool(
    default_phase: str = "planning",
    logger_name: str = "PlannerHITL",
):
    """Factory that creates a ``request_human_input`` tool bound to a specific phase.

    Args:
        default_phase: Default workflow phase if not provided at call time.
        logger_name: Logger name for this tool's log messages.

    Returns:
        A LangChain tool that triggers ``interrupt()`` for HITL.
    """
    hitl_logger = AgentLogger(logger_name)

    class HITLInputSchema(BaseModel):
        """Schema for request_human_input tool."""
        question: str = Field(description="The question or request for the human")
        context: Optional[str] = Field(default=None, description="Optional context about why feedback is needed")
        phase: Optional[str] = Field(default=None, description="Optional current workflow phase")

    @tool(args_schema=HITLInputSchema)
    def request_human_input(
        question: str,
        context: Optional[str] = None,
        phase: Optional[str] = None,
        runtime: Optional[ToolRuntime[None, HelmPlannerState]] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """Request human input during planning workflow execution.

        **CRITICAL: This is the ONLY way to request human input.**
        This tool pauses execution and waits for human input. Use when:
        - You need clarification on ambiguous requirements
        - You need approval for a decision
        - You need human input to proceed

        Args:
            question: The question or request for the human
            context: Optional context about why feedback is needed
            phase: Optional current workflow phase
        """
        # Get state context
        session_id = "unknown"
        task_id = "unknown"
        if runtime:
            if not phase:
                phase = runtime.state.get("current_step", default_phase)
            session_id = runtime.state.get("session_id", "unknown")
            task_id = runtime.state.get("task_id", "unknown")
        phase = phase or default_phase

        # Build interrupt payload
        interrupt_payload = {
            "pending_feedback_requests": {
                "status": "input_required",
                "session_id": session_id,
                "question": question,
                "context": context or "No additional context provided",
                "active_phase": phase,
                "tool_name": "request_human_input",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }

        hitl_logger.info(
            "Requesting human feedback",
            extra={
                "phase": phase,
                "question_preview": question[:100],
                "session_id": session_id,
            },
        )

        # Pause execution — interrupt() returns human response
        human_response = interrupt(interrupt_payload)
        human_response_str = str(human_response) if human_response else ""

        # Tool message responds to the assistant's tool call. 
        # MUST contain the user's feedback so the LLM reads it.
        tool_message = ToolMessage(
            content=f"User has provided feedback:\n{human_response_str}\n\nProceed with the workflow.",
            tool_call_id=tool_call_id,
        )

        # Safely extract existing state if available
        old_questions = runtime.state.get("question_asked", "") if runtime and hasattr(runtime, "state") else ""
        new_question = f"{old_questions}\n\n{question}".strip() if old_questions else question
        
        old_reqs = runtime.state.get("updated_user_requirements", "") if runtime and hasattr(runtime, "state") else ""
        
        current_discussion = (
            f"**question_asked**:\n{question}\n\n"
            f"**user_replied**:\n{human_response_str}"
        )
        
        new_reqs = f"{old_reqs}\n\n---\n\n{current_discussion}".strip() if old_reqs else current_discussion

        return Command(
            update={
                "messages": [tool_message],
                "updated_user_requirements": new_reqs,
                "question_asked": new_question,
            },
        )

    return request_human_input
