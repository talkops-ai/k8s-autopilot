"""Goal tools middleware for notice-based goal/rubric orientation.

Exposes constrained goal tools (``get_goal``, ``get_rubric``, ``update_goal``)
and keeps the model oriented via **goal-state notices**: lightweight internal
messages appended to checkpointed history whenever goal/rubric state changes.

The notice mechanism has two complementary halves:

- **Durable** (``before_model``): persists a fresh notice into the checkpoint
  when the latest one no longer matches authoritative state.
- **Transient** (``wrap_model_call``): re-pins the notice into the model
  request when the persisted one has scrolled out of the visible window (e.g.
  after summarization). This pin is request-only; it does not write to the
  checkpoint.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
)

from k8s_autopilot.middleware.goal_state_notice import (
    build_goal_state_notice,
    goal_state_fingerprint,
    has_goal_or_rubric_state,
    latest_goal_state_message_index,
    latest_goal_state_notice,
    latest_human_is_unsaved_goal_continuation,
)
from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.tools.goal_tools import (
    GOAL_TOOL_NAMES,
    GoalToolState,
    get_goal,
    get_rubric,
    propose_goal,
    update_goal,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langchain_core.messages import HumanMessage
    from langgraph.runtime import Runtime

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["GoalToolsMiddleware"]

ResponseT = TypeVar("ResponseT")


# ---------------------------------------------------------------------------
# Notice builder
# ---------------------------------------------------------------------------


def _goal_state_notice_for(
    state: dict[str, Any],
    messages: Sequence[object],
) -> HumanMessage | None:
    """Build a notice when effective history lacks current goal/rubric state.

    Args:
        state: Authoritative middleware state.
        messages: Messages visible at the next model boundary.

    Returns:
        Current notice to append, or ``None`` when history is already
        authoritative.
    """
    if latest_human_is_unsaved_goal_continuation(messages):
        return None
    latest = latest_goal_state_notice(messages)
    latest_candidate = latest_goal_state_message_index(messages)
    fingerprint = goal_state_fingerprint(state)
    if (
        latest is not None
        and latest[0] == latest_candidate
        and latest[1]["state_fingerprint"] == fingerprint
    ):
        return None
    if latest_candidate is None and not has_goal_or_rubric_state(state):
        return None
    return build_goal_state_notice(state)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@register_middleware(name="goal_tools")
class GoalToolsMiddleware(AgentMiddleware[GoalToolState, ContextT]):
    """Expose constrained goal tools and maintain the goal-state notice.

    Besides registering ``get_goal``/``get_rubric``/``update_goal``/``propose_goal``, this
    middleware keeps the model oriented at each model boundary:
    ``before_model`` persists a fresh goal-state notice into checkpointed
    history when the latest one no longer matches authoritative state, and
    ``wrap_model_call`` re-pins the notice into the (post-summarization)
    request when the persisted one is out of view.
    """

    state_schema = GoalToolState

    def __init__(self) -> None:
        """Initialize with goal tools."""
        super().__init__()
        self.tools = [get_rubric, get_goal, update_goal, propose_goal]

    # ------------------------------------------------------------------
    # Notice computation (shared by sync/async before_model)
    # ------------------------------------------------------------------

    @staticmethod
    def _notice_update(state: AgentState[Any]) -> dict[str, Any] | None:
        """Compute the checkpointed notice update for a ``before_model`` boundary.

        Returns:
            A ``messages`` update carrying a fresh notice, or ``None`` when
            history already reflects current goal/rubric state.
        """
        values = cast("dict[str, Any]", state)
        raw_messages = values.get("messages", [])
        messages = list(raw_messages) if isinstance(raw_messages, list) else []
        notice = _goal_state_notice_for(values, messages)
        return {"messages": [notice]} if notice is not None else None

    # ------------------------------------------------------------------
    # Durable half: persist notice into checkpoint
    # ------------------------------------------------------------------

    def before_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Persist a current goal-state notice into checkpointed history.

        This is the durable half of the notice mechanism; the transient
        counterpart in ``wrap_model_call`` re-pins the notice into a request
        whose persisted notice has scrolled out of the model-visible window.

        Returns:
            Message update containing a current notice, or ``None`` when
            unchanged.
        """
        return self._notice_update(state)

    async def abefore_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Persist a current goal-state notice at an async model boundary.

        Returns:
            Message update containing a current notice, or ``None`` when
            unchanged.
        """
        return self._notice_update(state)

    # ------------------------------------------------------------------
    # Transient half: re-pin notice into model request
    # ------------------------------------------------------------------

    @staticmethod
    def _request_with_goal_notice(
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """Re-pin the current goal-state notice into a model request when needed.

        When checkpointed history no longer surfaces a current notice, a
        transient goal-state notice is appended to the request messages only
        (not persisted; ``before_model`` owns the durable write).

        Returns:
            The original request when no notice is needed, otherwise a request
            with a current goal-state notice appended to its messages.
        """
        values = cast("dict[str, Any]", request.state)
        notice = _goal_state_notice_for(values, request.messages)
        if notice is None:
            return request
        return request.override(messages=[*request.messages, notice])

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Re-pin the goal-state notice into each model request when needed.

        Returns:
            Model response from the wrapped handler.
        """
        return handler(self._request_with_goal_notice(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """Re-pin the goal-state notice into each async model request when needed.

        Returns:
            Model response from the wrapped handler.
        """
        return await handler(self._request_with_goal_notice(request))
