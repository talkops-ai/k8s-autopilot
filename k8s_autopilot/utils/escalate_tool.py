"""
Escalation tool for coordinator deep agents.

When a coordinator deep agent receives a follow-up request that falls outside
its scope (e.g. the helm operator gets asked about pod logs), the LLM calls
``escalate_to_supervisor`` instead of writing a free-text refusal.

The tool returns a ``Command`` that sets a structured ``escalation_request``
marker in the deep agent's state.  The coordinator wrapper node in the
supervisor (``_make_coordinator_node``) detects this marker in ``final_state``
and translates it into:

    - ``dialog_state: "pop"``   — remove current agent from the stack
    - ``status: "pending"``     — trigger re-classification
    - ``user_query: <request>`` — the user's actual follow-up request

This allows the supervisor's ``_route_after_supervisor`` to route via
``classify_request`` without any string matching.

Architecture reference:
    - §Routing model, Mode A: global stack-based routing
    - §Routing algorithm step 3: empty stack + pending → classify_request
    - §Non-negotiable: cross-domain transfers use structured envelopes
"""

from typing import Annotated, Any
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("EscalateTool")


def create_escalate_to_supervisor_tool():
    """Factory that returns an escalation tool for coordinator deep agents.

    Usage::

        from k8s_autopilot.utils.escalate_tool import create_escalate_to_supervisor_tool

        escalate = create_escalate_to_supervisor_tool()
        tools = [sync_workspace, user_input, chat_continue, log_operation, escalate]
    """

    @tool
    async def escalate_to_supervisor(
        user_request: str,
        reason: str,
        runtime: ToolRuntime[None, Any],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Escalate a user request back to the supervisor for re-routing.

        Call this tool when the user's request is OUTSIDE YOUR SCOPE and needs
        to be handled by a different coordinator agent (k8s operator,
        app operator, observability operator, etc.).

        IMPORTANT: Do NOT refuse the user in free text.  Always call this tool
        instead so the supervisor can route the request to the correct agent.

        Args:
            user_request: The user's exact request that is outside your scope.
            reason: Brief explanation of why this request is outside your scope.
        """
        logger.info(
            "Escalation to supervisor requested",
            extra={
                "user_request_preview": user_request[:200],
                "reason": reason[:200],
            },
        )

        return Command(
            goto="__end__",
            update={
                "escalation_request": {
                    "user_request": user_request,
                    "reason": reason,
                },
                "messages": [
                    ToolMessage(
                        content="Escalation request submitted successfully.",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    return escalate_to_supervisor
