"""Schema and middleware for per-checkpoint state restored when resuming.

``ResumeState`` declares several checkpointed, schema-private channels:

Written from inside the graph on successful model turns:
- ``_context_tokens`` — total context tokens from the latest
    ``AIMessage.usage_metadata``.
- ``_model_spec`` / ``_model_params`` — the model and invocation params
    effectively in use for the turn.

Written through the main graph or by the client via ``aupdate_state``:
- ``_goal_objective`` / ``_goal_status`` / ``_goal_rubric`` / ``_goal_status_note``
- ``_pending_goal_completion_note``
- ``_sticky_rubric``
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    cast,
    get_args,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage

from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.state.goal_channels import (
    GoalProposalKind,
    GoalRubricChannels,
    GoalStatus,
    RUBRIC_RESULT_VALUES,
    _flatten_literal_values,
    coerce_goal_proposal_kind,
    coerce_goal_status,
)

if TYPE_CHECKING:
    from langgraph.runtime import Runtime


class ResumeState(GoalRubricChannels):
    """Extends agent state with per-checkpoint facts restored on resume.

    Inherits the shared goal/rubric channels from ``GoalRubricChannels`` and
    adds the channels unique to resume: the after-model token/spec facts.
    """

    _context_tokens: Annotated[NotRequired[int], PrivateStateAttr]
    _model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    _model_params: Annotated[NotRequired[dict[str, Any] | None], PrivateStateAttr]


def _extract_context_tokens(message: AIMessage) -> int | None:
    """Return the context-token count from an AI message, or ``None`` if absent."""
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None
    input_toks = usage.get("input_tokens", 0) or 0
    output_toks = usage.get("output_tokens", 0) or 0
    if input_toks or output_toks:
        return input_toks + output_toks
    total = usage.get("total_tokens", 0) or 0
    return total or None


@register_middleware(name="resume_state")
class ResumeStateMiddleware(AgentMiddleware[ResumeState, ContextT]):
    """Persists per-checkpoint resume facts after each model call."""

    state_schema = ResumeState

    def after_model(
        self,
        state: ResumeState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Write ``_context_tokens`` for the latest turn."""
        update: dict[str, Any] = {}

        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, AIMessage):
                tokens = _extract_context_tokens(msg)
                if tokens is not None:
                    update["_context_tokens"] = tokens
                break

        return update or None
