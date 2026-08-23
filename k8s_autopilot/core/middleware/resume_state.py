"""Schema and middleware for per-checkpoint state restored when resuming."""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
from typing import TYPE_CHECKING, Any, Annotated, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = AgentLogger("ResumeStateMW")


# ---------------------------------------------------------------------------
# Inline Schemas
# ---------------------------------------------------------------------------

class ResumeState(AgentState):
    """Extends agent state with per-checkpoint facts restored on resume."""
    _context_tokens: Annotated[NotRequired[int], PrivateStateAttr]
    _model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    _model_params: Annotated[NotRequired[dict[str, Any] | None], PrivateStateAttr]


def _extract_context_tokens(msg: AIMessage) -> int | None:
    usage = msg.usage_metadata or {}
    total = usage.get("total_tokens", 0) or 0
    return total or None


from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware


@register_middleware(name="resume_state")
class ResumeStateMiddleware(BaseAgentMiddleware):
    """Persists per-checkpoint resume facts after each model call."""

    state_schema = ResumeState

    def after_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Write `_context_tokens` for the latest turn."""
        update: dict[str, Any] = {}

        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, AIMessage):
                tokens = _extract_context_tokens(msg)
                if tokens is not None:
                    update["_context_tokens"] = tokens
                break

        return update or None
