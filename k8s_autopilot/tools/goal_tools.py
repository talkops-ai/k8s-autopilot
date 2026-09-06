"""Goal management tools exposed to the agent for persisted goals.

Ported from ``reference/opscode/src/opscode/tools/goal_tools.py``.

These tools let the model inspect and update the goal lifecycle.
They operate on checkpointed ``PrivateStateAttr`` channels shared between the
``GoalToolsMiddleware`` (which registers them) and ``ResumeState`` (which
declares the channels).

The tools are intentionally constrained:

- ``get_goal`` / ``get_rubric`` are read-only projections.
- ``update_goal`` can only mark a goal ``complete`` or ``blocked``; creation,
  pausing, resuming, and clearing are user-controlled actions.
- Completion is **staged** via ``_pending_goal_completion_note`` rather than
  committed directly, so ``RubricMiddleware`` can verify acceptance criteria
  before the status change is recorded.
"""

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    TypedDict,
)

from langchain.agents.middleware.types import AgentState, PrivateStateAttr
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command, interrupt
from pydantic import Field

from k8s_autopilot.middleware.resume_state import (
    GoalRubricChannels,
    GoalStatus,
    coerce_goal_status,
)
from k8s_autopilot.rubrics.generator import generate_rubric

if TYPE_CHECKING:
    pass

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

GOAL_TOOL_NAMES = frozenset({"get_goal", "get_rubric", "update_goal", "propose_goal"})
"""Tool names used by behavioral absence gates and middleware contract tests."""


# ---------------------------------------------------------------------------
# State projection used by tools
# ---------------------------------------------------------------------------


class GoalToolState(GoalRubricChannels):
    """State fields used by goal tools.

    Inherits the shared ``_goal_*``/``_sticky_rubric`` channels (with their
    ``PrivateStateAttr`` markers) from ``GoalRubricChannels``, so the goal tools
    and ``ResumeState`` cannot drift apart. Adds only the public ``rubric``
    graph input, which is intentionally non-private — it is the
    ``RubricMiddleware`` input.
    """

    rubric: NotRequired[str | None]
    """Public ``RubricMiddleware`` graph input (intentionally non-private).

    Distinct from the TUI-owned ``_sticky_rubric``: this is the per-invocation
    rubric passed in via the graph schema, not checkpointed state.
    """


# ---------------------------------------------------------------------------
# Snapshot types
# ---------------------------------------------------------------------------


class RubricSnapshot(TypedDict):
    """Read-only rubric view returned by the ``get_rubric`` tool to the model.

    ``active`` is always ``criteria is not None``; the two never disagree.
    """

    active: bool
    """Whether acceptance criteria are currently available."""

    criteria: str | None
    """Current acceptance criteria, or ``None`` when no rubric is set."""

    grading_status: str | None
    """Latest ``RubricMiddleware`` grading status for the in-progress or
    just-completed graded turn, or ``None``."""


class GoalSnapshot(TypedDict):
    """Read-only goal view returned by the ``get_goal`` tool to the model.

    A fixed-shape projection of goal state. Both construction branches in
    ``_goal_snapshot`` must populate every key, so the type checker catches a
    drift between them.
    """

    active: bool
    """Whether the goal is actionable (should drive work).

    Derived from ``status``: ``active`` and ``blocked`` goals are actionable,
    while ``paused`` and ``complete`` goals are not.
    """

    objective: str | None
    """Active goal objective, or ``None`` when no goal is set."""

    status: GoalStatus | None
    """Lifecycle status, or ``None`` when no goal is set."""

    criteria: str | None
    """Persisted goal criteria, or shared rubric criteria when no goal rubric
    exists."""

    note: str | None
    """Persisted completion evidence or blocker note for the goal."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_state_text(state: dict[str, Any], key: str) -> str | None:
    """Return a non-empty stripped string from ``state[key]``, or ``None``.

    Used by snapshot builders to normalize empty strings and whitespace-only
    values to ``None`` so the model sees a clean signal.
    """
    value = state.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------


def _rubric_snapshot(state: dict[str, Any]) -> RubricSnapshot:
    """Build the ``get_rubric`` response from graph state.

    Criteria resolve in precedence order: public ``rubric`` input, else an
    actionable goal rubric, else a standalone sticky rubric.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        Rubric snapshot visible to the model.
    """
    criteria = _clean_state_text(state, "rubric")
    goal_rubric = _clean_state_text(state, "_goal_rubric")
    sticky_rubric = _clean_state_text(state, "_sticky_rubric")
    objective = _clean_state_text(state, "_goal_objective")
    status = coerce_goal_status(state.get("_goal_status")) or "active"
    goal_is_actionable = objective is not None and status in {"active", "blocked"}
    sticky_is_goal_rubric = objective is not None and sticky_rubric == goal_rubric

    # Prefer the public `rubric` graph input when present; otherwise surface
    # actionable goal criteria or a standalone sticky rubric.
    if criteria is None:
        if goal_is_actionable and goal_rubric is not None:
            criteria = goal_rubric
        elif sticky_rubric is not None and not sticky_is_goal_rubric:
            criteria = sticky_rubric

    grading_status = _clean_state_text(state, "_rubric_status")
    return {
        "active": criteria is not None,
        "criteria": criteria,
        "grading_status": grading_status,
    }


def _goal_snapshot(state: dict[str, Any]) -> GoalSnapshot:
    """Build the ``get_goal`` response from graph state.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        Goal snapshot visible to the model.
    """
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
    criteria = _clean_state_text(state, "_goal_rubric") or rubric["criteria"]
    note = _clean_state_text(state, "_goal_status_note")
    return {
        "active": status in {"active", "blocked"},
        "objective": objective,
        "status": status,
        "criteria": criteria,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Update command builder
# ---------------------------------------------------------------------------


def _update_goal_command(
    *,
    status: Literal["complete", "blocked"],
    note: str,
    tool_call_id: str,
    state: dict[str, Any],
) -> Command[Any]:
    """Build the constrained ``update_goal`` command.

    Args:
        status: Goal status the model is reporting (``complete`` or ``blocked``).
        note: Evidence the goal is complete, or the specific blocker.
        tool_call_id: Tool call ID for the returned ``ToolMessage``.
        state: Current graph state injected by LangGraph.

    Returns:
        Command updating goal metadata and returning a tool response.
    """
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
    goal_status = coerce_goal_status(state.get("_goal_status")) or "active"
    if goal_status in {"paused", "complete"}:
        if goal_status == "paused":
            message = (
                "The goal is paused. The user must run `/goal resume` before its "
                "status can be updated."
            )
        else:
            message = "The goal is already complete and cannot be updated."
        return Command(
            update={
                "messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]
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
        # Stage completion evidence; resolved after rubric grader's verdict.
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
    # Blocked — commit immediately and clear any pending completion staging.
    update: dict[str, Any] = {
        "_goal_status": status,
        "_goal_status_note": clean_note,
        "_pending_goal_completion_note": None,
    }
    return Command(
        update={
            **update,
            "messages": [
                ToolMessage(
                    content=f"Goal marked {status}. {clean_note}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


# ---------------------------------------------------------------------------
# Tool definitions (registered by GoalToolsMiddleware.__init__)
# ---------------------------------------------------------------------------


@tool
def get_rubric(
    state: Annotated[dict[str, Any], InjectedState],
) -> RubricSnapshot:
    """Read criteria when the latest state notice says a rubric is active.

    Use this only when the latest goal/rubric state notice reports an active
    rubric. Use ``get_goal`` when a goal is actionable; this tool only reports
    whether criteria are active, the current criteria, and the latest grading
    status.

    Returns:
        Rubric snapshot with ``active``, ``criteria``, and ``grading_status``
        keys.
    """
    return _rubric_snapshot(state)


@tool
def get_goal(
    state: Annotated[dict[str, Any], InjectedState],
) -> GoalSnapshot:
    """Read a goal when the latest state notice says it is actionable.

    Use this only when the latest goal/rubric state notice reports an
    actionable goal. It returns the objective, criteria, lifecycle status,
    and any prior note from authoritative checkpoint state.

    Returns:
        Goal snapshot with ``active``, ``objective``, ``status``, ``criteria``,
        and ``note`` keys.
    """
    return _goal_snapshot(state)


@tool
def update_goal(
    status: Annotated[
        Literal["complete", "blocked"],
        Field(
            description=(
                "`complete` to attach completion evidence, or `blocked` "
                "when you are stuck and need the user."
            )
        ),
    ],
    note: Annotated[
        str,
        Field(
            description=(
                "Evidence the criteria are satisfied, or the specific "
                "blocker. Required when calling this tool."
            )
        ),
    ],
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict[str, Any], InjectedState],
) -> Command[Any]:
    """Update a goal only when the latest state notice says it is actionable.

    Use ``blocked`` when you cannot proceed without user input. Goals complete
    automatically after a satisfied goal-backed grading turn, so ``complete``
    is optional and only stages its evidence for that result.

    Returns:
        Command that updates goal status and returns a tool message.
    """
    return _update_goal_command(
        status=status,
        note=note,
        tool_call_id=tool_call_id,
        state=state,
    )


def _parse_goal_response(
    response: object,
    objective: str,
    criteria: list[str],
    tool_call_id: str,
) -> Command[Any]:
    """Parse user review decision from interrupt response and update goal state."""
    import json
    from k8s_autopilot.rubrics.generator import generate_rubric

    parsed = response
    if isinstance(response, str):
        trimmed = response.strip()
        if (trimmed.startswith("{") and trimmed.endswith("}")) or (
            trimmed.startswith("[") and trimmed.endswith("]")
        ):
            try:
                parsed = json.loads(trimmed)
            except Exception:
                parsed = {"decision": response}
        else:
            parsed = {"decision": response}

    decision = "confirm"
    feedback = ""
    effective_criteria = list(criteria)

    if isinstance(parsed, dict):
        raw_decision = (
            parsed.get("decision")
            or parsed.get("action")
            or parsed.get("status")
            or "confirm"
        )
        decision = str(raw_decision).lower().strip()
        if "criteria" in parsed and parsed["criteria"]:
            raw_c = parsed["criteria"]
            if isinstance(raw_c, list):
                effective_criteria = [str(x).strip() for x in raw_c if str(x).strip()]
            elif isinstance(raw_c, str):
                effective_criteria = [
                    line.strip().lstrip("-* ").strip()
                    for line in raw_c.splitlines()
                    if line.strip().lstrip("-* ").strip()
                ]
        feedback = str(parsed.get("feedback") or parsed.get("message") or "")
    elif isinstance(parsed, str):
        decision = parsed.lower().strip()

    rubric_str = "\n".join(f"- {c}" for c in effective_criteria)

    if decision in ("accept", "confirm", "accepted", "confirmed", "y", "yes"):
        result_text = (
            f"Goal confirmed by user.\n"
            f"**Objective:** {objective}\n"
            f"**Criteria:**\n{rubric_str}\n\n"
            "The goal and rubric are now active. Goal-state notices will orient subsequent turns, "
            "and execution will be evaluated against this rubric."
        )
        return Command(
            update={
                "_goal_objective": objective,
                "_goal_status": "active",
                "_goal_rubric": rubric_str,
                "rubric": rubric_str,
                "_goal_status_note": None,
                "_pending_goal_completion_note": None,
                "messages": [
                    ToolMessage(
                        result_text,
                        tool_call_id=tool_call_id,
                        name="propose_goal",
                    )
                ],
            }
        )
    elif decision in ("edit", "edited", "e"):
        result_text = (
            f"Goal confirmed with user-edited criteria.\n"
            f"**Objective:** {objective}\n"
            f"**Criteria:**\n{rubric_str}\n\n"
            "The goal and revised rubric are now active. Goal-state notices will orient subsequent turns, "
            "and execution will be evaluated against this rubric."
        )
        return Command(
            update={
                "_goal_objective": objective,
                "_goal_status": "active",
                "_goal_rubric": rubric_str,
                "rubric": rubric_str,
                "_goal_status_note": None,
                "_pending_goal_completion_note": None,
                "messages": [
                    ToolMessage(
                        result_text,
                        tool_call_id=tool_call_id,
                        name="propose_goal",
                    )
                ],
            }
        )
    elif decision in ("reject", "rejected", "r"):
        # Regenerate criteria using GOAL_RUBRIC_SYSTEM_PROMPT with the user's rejection feedback
        regenerated_criteria = ""
        try:
            regenerated_criteria = generate_rubric(
                objective,
                feedback=feedback,
                previous_criteria=rubric_str,
            )
        except Exception as exc:
            logger.warning("Failed to regenerate rubric criteria: %s", exc)

        result_text = (
            f"User rejected proposed goal criteria with feedback:\n"
            f"{feedback or '(No feedback provided)'}\n\n"
        )
        if regenerated_criteria:
            result_text += (
                f"Regenerated Criteria based on feedback:\n{regenerated_criteria}\n\n"
                "Please review the regenerated criteria with the user or adjust your plan."
            )
        else:
            result_text += "Please revise the criteria or ask the user for further clarification."

        return Command(
            update={
                "messages": [
                    ToolMessage(
                        result_text,
                        tool_call_id=tool_call_id,
                        name="propose_goal",
                    )
                ],
            }
        )
    else:  # cancel / dismiss / n
        result_text = (
            "User dismissed the goal proposal. Proceed with addressing "
            "the user's request directly without an active goal rubric."
        )
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        result_text,
                        tool_call_id=tool_call_id,
                        name="propose_goal",
                    )
                ],
            }
        )


@tool
def propose_goal(
    objective: Annotated[
        str,
        Field(description="The high-level goal objective to accomplish."),
    ],
    criteria: Annotated[
        list[str] | str | None,
        Field(
            default=None,
            description=(
                "Optional list of 2-5 concise, verifiable, outcome-focused acceptance criteria "
                "bullets. If omitted, criteria will be automatically drafted using the criteria agent."
            ),
        ),
    ] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command[Any]:
    """Propose a formal goal with verifiable acceptance criteria for interactive user review.

    When given a non-trivial, multi-step, or architectural task (e.g. setting up monitoring,
    deploying Helm applications, provisioning clusters, multi-file migrations), use this tool
    to draft and present acceptance criteria for user confirmation before making changes.
    The criteria agent will generate concrete, outcome-focused checklist criteria that become
    the active rubric once accepted by the user.

    Returns:
        Command that pauses for user review and establishes the active goal upon acceptance.
    """
    raw_criteria: list[Any] | str | None = criteria
    if not raw_criteria:
        try:
            rubric_text = generate_rubric(objective)
            raw_criteria = [
                line.strip().lstrip("-* ").strip()
                for line in rubric_text.splitlines()
                if line.strip().lstrip("-* ").strip()
            ]
        except Exception as exc:
            logger.warning("Failed to auto-generate rubric for propose_goal: %s", exc)
            raw_criteria = [f"Complete objective: {objective}"]

    # Clean and filter criteria items
    criteria_list: list[str] = []
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if isinstance(item, dict):
                if item.get("type") in {"thinking", "reasoning", "thought"}:
                    continue
                val = item.get("text") or item.get("criterion") or item.get("criteria") or str(item)
                clean_val = str(val).strip().lstrip("-*•0123456789.) ")
                if clean_val:
                    criteria_list.append(clean_val)
            elif isinstance(item, str):
                s = item.strip()
                if s.startswith("{") and s.endswith("}"):
                    try:
                        import json
                        d = json.loads(s)
                        c = d.get("criteria") or d.get("text") or s
                        if isinstance(c, list):
                            for sub in c:
                                criteria_list.append(str(sub).strip().lstrip("-*•0123456789.) "))
                            continue
                        elif isinstance(c, str):
                            for sub in c.splitlines():
                                clean_sub = sub.strip().lstrip("-*•0123456789.) ")
                                if clean_sub:
                                    criteria_list.append(clean_sub)
                            continue
                    except Exception:
                        pass
                clean_s = s.lstrip("-*•0123456789.) ").strip()
                if clean_s and not clean_s.startswith(("{", "}", "[", "]")):
                    criteria_list.append(clean_s)
    elif isinstance(raw_criteria, str):
        for line in raw_criteria.splitlines():
            clean_l = line.strip().lstrip("-*•0123456789.) ").strip()
            if clean_l and not clean_l.startswith(("{", "}", "[", "]")):
                criteria_list.append(clean_l)

    if not criteria_list:
        criteria_list = [f"Complete objective: {objective}"]

    review_request = {
        "type": "goal_review",
        "objective": objective,
        "criteria": criteria_list,
        "tool_call_id": tool_call_id,
    }
    response = interrupt(review_request)
    return _parse_goal_response(response, objective, criteria_list, tool_call_id)


