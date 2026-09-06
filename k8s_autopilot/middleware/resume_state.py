"""Schema and middleware for per-checkpoint state restored when resuming.

Ported from ``reference/opscode/src/opscode/middleware/resume_state.py``.

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

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

GoalStatus = Literal["active", "paused", "blocked", "complete"]
"""Lifecycle status of a goal."""

GoalProposalKind = Literal["create", "amend"]
"""Whether a pending review creates a goal or amends the current one."""

_GOAL_STATUS_VALUES: frozenset[str] = frozenset(get_args(GoalStatus))
_GOAL_PROPOSAL_KIND_VALUES: frozenset[str] = frozenset(get_args(GoalProposalKind))


def _flatten_literal_values(tp: object) -> frozenset[str]:
    """Collect every string value from a (possibly unioned) ``Literal`` type."""
    values: set[str] = set()
    for arg in get_args(tp):
        if isinstance(arg, str):
            values.add(arg)
        else:
            values |= _flatten_literal_values(arg)
    return frozenset(values)


try:
    from deepagents.middleware.rubric import RubricResult

    RUBRIC_RESULT_VALUES: frozenset[str] = _flatten_literal_values(RubricResult)
except ImportError:
    RUBRIC_RESULT_VALUES = frozenset({"satisfied", "not_satisfied"})


def coerce_goal_proposal_kind(value: object) -> GoalProposalKind | None:
    """Narrow a persisted proposal kind to a known value."""
    if isinstance(value, str) and value in _GOAL_PROPOSAL_KIND_VALUES:
        return cast("GoalProposalKind", value)
    return None


def coerce_goal_status(value: object) -> GoalStatus | None:
    """Narrow a persisted goal-status value to a known ``GoalStatus``."""
    if isinstance(value, str) and value in _GOAL_STATUS_VALUES:
        return cast("GoalStatus", value)
    return None


class GoalRubricChannels(AgentState):
    """Goal/rubric state channels shared by every schema that touches them."""

    _goal_objective: Annotated[NotRequired[str | None], PrivateStateAttr]
    _goal_status: Annotated[NotRequired[GoalStatus | None], PrivateStateAttr]
    _goal_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]
    _goal_status_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    _pending_goal_completion_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    _sticky_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]


class ResumeState(GoalRubricChannels):
    """Extends agent state with per-checkpoint facts restored on resume."""

    _context_tokens: Annotated[NotRequired[int], PrivateStateAttr]
    _model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    _model_params: Annotated[NotRequired[dict[str, Any] | None], PrivateStateAttr]
    _pending_goal_objective: Annotated[NotRequired[str | None], PrivateStateAttr]
    _pending_goal_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]
    _pending_goal_kind: Annotated[NotRequired[GoalProposalKind | None], PrivateStateAttr]
    _pending_goal_request_id: Annotated[NotRequired[str | None], PrivateStateAttr]


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
