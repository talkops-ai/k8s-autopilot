"""Canonical goal and rubric state channels for K8s Autopilot.

``GoalRubricChannels`` declares the single source of truth for checkpointed,
schema-private channels used by both ``ResumeStateMiddleware`` and
``goal_tools.GoalToolState``.
"""

from __future__ import annotations

from typing import (
    Annotated,
    Literal,
    NotRequired,
    cast,
    get_args,
)

from langchain.agents.middleware.types import (
    AgentState,
    PrivateStateAttr,
)

GoalStatus = Literal["active", "paused", "blocked", "complete"]
"""Lifecycle status of a goal.

``active`` and ``blocked`` are actionable working states; ``paused`` preserves
the goal without driving work; ``complete`` is terminal.
"""

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
    """Canonical goal/rubric state channels shared by schemas touching them.

    Declared once here so schemas that carry these channels — ``ResumeState`` and
    ``GoalToolState`` — inherit the exact same ``PrivateStateAttr`` annotations.
    """

    _goal_objective: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Accepted goal objective restored on resume."""

    _goal_status: Annotated[NotRequired[GoalStatus | None], PrivateStateAttr]
    """Goal status (``active``, ``paused``, ``blocked``, ``complete``)."""

    _goal_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Accepted rubric criteria associated with ``_goal_objective``."""

    _goal_status_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Persisted completion evidence or blocker note for the goal."""

    _pending_goal_completion_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Optional agent-provided completion evidence awaiting rubric grading."""

    _sticky_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Persistent rubric criteria owned by the session/TUI."""

    _pending_goal_objective: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Goal objective awaiting acceptance of proposed criteria."""

    _pending_goal_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Proposed criteria awaiting user acceptance."""

    _pending_goal_kind: Annotated[
        NotRequired[GoalProposalKind | None], PrivateStateAttr,
    ]
    """Whether the pending review creates or amends a goal."""

    _pending_goal_request_id: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Request ID that produced the pending proposal."""


__all__ = [
    "RUBRIC_RESULT_VALUES",
    "GoalProposalKind",
    "GoalRubricChannels",
    "GoalStatus",
    "coerce_goal_proposal_kind",
    "coerce_goal_status",
]
