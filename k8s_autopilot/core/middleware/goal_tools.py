"""Goal tools exposed to the agent for persisted TUI goals."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    TypedDict,
    TypeVar,
    cast,
    get_args,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

RubricSource = Literal["goal", "sticky", "invocation"]
GoalStatus = Literal["active", "blocked", "complete"]

_GOAL_STATUS_VALUES: frozenset[str] = frozenset(get_args(GoalStatus))


def coerce_goal_status(value: object) -> GoalStatus | None:
    if isinstance(value, str) and value in _GOAL_STATUS_VALUES:
        return cast("GoalStatus", value)
    return None


class GoalRubricChannels(AgentState):
    _goal_objective: Annotated[NotRequired[str | None], PrivateStateAttr]
    _goal_status: Annotated[NotRequired[GoalStatus | None], PrivateStateAttr]
    _goal_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]
    _goal_status_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    _pending_goal_completion_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    _sticky_rubric: Annotated[NotRequired[str | None], PrivateStateAttr]


class GoalToolState(GoalRubricChannels):
    rubric: NotRequired[str | None]
    _rubric_status: Annotated[NotRequired[str | None], PrivateStateAttr]


GOAL_TOOLS_SYSTEM_PROMPT = """## Goal and Rubric Tools

Use `get_rubric` to inspect active acceptance criteria before deciding whether work is
complete.
When a goal is active, use `get_goal` to inspect the objective and current status.
Use `update_goal` only when you have evidence that the goal is complete or blocked."""

ResponseT = TypeVar("ResponseT")


def _runtime_blocked_goal_retry_context(ctx: object) -> str | None:
    if isinstance(ctx, dict):
        value = ctx.get("blocked_goal_retry_context")
    else:
        value = getattr(ctx, "blocked_goal_retry_context", None)
    return value if isinstance(value, str) and value else None


class RubricSnapshot(TypedDict):
    active: bool
    criteria: str | None
    source: RubricSource | None
    grading_status: str | None


class GoalSnapshot(TypedDict):
    active: bool
    objective: str | None
    status: GoalStatus | None
    criteria: str | None
    note: str | None


def _clean_state_text(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _rubric_snapshot(state: dict[str, Any]) -> RubricSnapshot:
    criteria = _clean_state_text(state, "rubric")
    goal_rubric = _clean_state_text(state, "_goal_rubric")
    sticky_rubric = _clean_state_text(state, "_sticky_rubric")
    objective = _clean_state_text(state, "_goal_objective")

    source: RubricSource | None = None
    if criteria is not None:
        if objective is not None and goal_rubric == criteria:
            source = "goal"
        elif sticky_rubric == criteria:
            source = "sticky"
        else:
            source = "invocation"
    elif objective is not None and goal_rubric is not None:
        criteria = goal_rubric
        source = "goal"
    elif sticky_rubric is not None:
        criteria = sticky_rubric
        source = "sticky"

    grading_status = _clean_state_text(state, "_rubric_status")
    return {
        "active": criteria is not None,
        "criteria": criteria,
        "source": source,
        "grading_status": grading_status,
    }


def _goal_snapshot(state: dict[str, Any]) -> GoalSnapshot:
    objective = _clean_state_text(state, "_goal_objective")
    rubric = _rubric_snapshot(state)
    if objective is None:
        return {
            "active": False,
            "objective": None,
            "status": None,
            "criteria": rubric["criteria"],
            "note": None,
        }
    status: GoalStatus = coerce_goal_status(state.get("_goal_status")) or "active"
    note = _clean_state_text(state, "_goal_status_note")
    return {
        "active": status != "complete",
        "objective": objective,
        "status": status,
        "criteria": rubric["criteria"],
        "note": note,
    }


def _update_goal_command(
    *,
    status: Literal["complete", "blocked"],
    note: str,
    tool_call_id: str,
    state: dict[str, Any],
) -> Command[Any]:
    objective = state.get("_goal_objective")
    if not isinstance(objective, str) or not objective:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="No active goal is set.",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    clean_note = note.strip()
    if not clean_note:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"Provide a note with evidence before marking the "
                            f"goal {status}."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    if status == "complete":
        return Command(
            update={
                "_pending_goal_completion_note": clean_note,
                "messages": [
                    ToolMessage(
                        content=(
                            "Goal completion requested. It will be recorded if "
                            "the accepted rubric is satisfied."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )
    return Command(
        update={
            "_goal_status": status,
            "_goal_status_note": clean_note,
            "_pending_goal_completion_note": None,
            "messages": [
                ToolMessage(
                    content=f"Goal marked {status}. {clean_note}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware


@register_middleware(name="goal_tools")
class GoalToolsMiddleware(BaseAgentMiddleware):
    """Expose constrained goal tools to the main agent."""

    state_schema = GoalToolState

    def __init__(self) -> None:
        super().__init__()

        @tool
        def get_rubric(
            state: Annotated[dict[str, Any], InjectedState],
        ) -> RubricSnapshot:
            """Read the current acceptance criteria used to evaluate completion."""
            return _rubric_snapshot(state)

        @tool
        def get_goal(
            state: Annotated[dict[str, Any], InjectedState],
        ) -> GoalSnapshot:
            """Read the current persistent goal and acceptance criteria."""
            return _goal_snapshot(state)

        @tool
        def update_goal(
            status: Literal["complete", "blocked"],
            note: str,
            tool_call_id: Annotated[str, InjectedToolCallId],
            state: Annotated[dict[str, Any], InjectedState],
        ) -> Command[Any]:
            """Mark the current goal complete or blocked with evidence."""
            return _update_goal_command(
                status=status,
                note=note,
                tool_call_id=tool_call_id,
                state=state,
            )

        self.tools = [get_rubric, get_goal, update_goal]

    @staticmethod
    def _request_with_goal_system_context(
        request: ModelRequest,
    ) -> ModelRequest:
        retry_context = _runtime_blocked_goal_retry_context(request.runtime.context)
        prompt_parts = [GOAL_TOOLS_SYSTEM_PROMPT]
        if retry_context is not None:
            prompt_parts.append(retry_context)
        prompt = "\n\n".join(prompt_parts)

        if request.system_message is not None:
            content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{prompt}"},
            ]
        else:
            content = [{"type": "text", "text": prompt}]
        return request.override(
            system_message=SystemMessage(
                content=cast("list[str | dict[str, str]]", content)
            )
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._request_with_goal_system_context(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._request_with_goal_system_context(request))
