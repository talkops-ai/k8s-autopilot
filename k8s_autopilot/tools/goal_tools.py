"""Goal management tools exposed to the agent for persisted goals.

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

from k8s_autopilot.state.goal_channels import (
    GoalRubricChannels,
    GoalStatus,
    coerce_goal_status,
)
from k8s_autopilot.schema.interrupts import GoalReviewResumePayload
import k8s_autopilot.rubrics.generator as rubric_generator
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
    payload = GoalReviewResumePayload.from_raw(response)
    effective_criteria = payload.criteria if payload.criteria is not None else list(criteria)
    rubric_str = "\n".join(f"- {c}" for c in effective_criteria)

    if payload.decision == "confirm":
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
                "_sticky_rubric": rubric_str,
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
    elif payload.decision == "edit":
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
                "_sticky_rubric": rubric_str,
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
    elif payload.decision == "reject":
        regenerated_obj, regenerated_bullets = draft_goal_criteria(
            objective,
            feedback=payload.feedback,
            previous_criteria=rubric_str,
        )
        regenerated_criteria = "\n".join(f"- {b}" for b in regenerated_bullets)

        result_text = (
            f"User rejected proposed goal criteria with feedback:\n"
            f"{payload.feedback or '(No feedback provided)'}\n\n"
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
    else:  # cancel
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



_active_criteria_agent: Any | None = None
_active_fallback_agent: Any | None = None


def set_active_criteria_agent(agent: Any | None, fallback: Any | None = None) -> None:
    """Register the active criteria agent and fallback agent for goal drafting."""
    global _active_criteria_agent, _active_fallback_agent
    _active_criteria_agent = agent
    _active_fallback_agent = fallback


def get_active_criteria_agent() -> tuple[Any | None, Any | None]:
    """Get the currently registered criteria agent and fallback agent."""
    return _active_criteria_agent, _active_fallback_agent


def draft_goal_criteria(
    objective: str,
    *,
    suggested_criteria: list[Any] | str | None = None,
    feedback: str | None = None,
    previous_criteria: str | None = None,
) -> tuple[str, list[str]]:
    """Draft and verify goal acceptance criteria using the criteria agent or rubric generator.

    If the criteria agent is active, runs the criteria agent (which uses repository inspection
    and MCP cluster tools) to formulate concrete, verifiable acceptance criteria.
    Falls back to generate_rubric or structured cleaning.
    """
    cleaned_suggestions: list[str] = []
    if suggested_criteria:
        if isinstance(suggested_criteria, list):
            for item in suggested_criteria:
                if isinstance(item, str):
                    s = item.strip().lstrip("-*•0123456789.) ").strip()
                    if s:
                        cleaned_suggestions.append(s)
                elif isinstance(item, dict):
                    val = item.get("text") or item.get("criterion") or item.get("criteria")
                    if val:
                        cleaned_suggestions.append(str(val).strip().lstrip("-*•0123456789.) ").strip())
        elif isinstance(suggested_criteria, str):
            for line in suggested_criteria.splitlines():
                s = line.strip().lstrip("-*•0123456789.) ").strip()
                if s:
                    cleaned_suggestions.append(s)

    seed_criteria_str = previous_criteria
    if not seed_criteria_str and cleaned_suggestions:
        seed_criteria_str = "\n".join(f"- {c}" for c in cleaned_suggestions)

    # 1. Try invoking the active criteria agent
    global _active_criteria_agent, _active_fallback_agent
    if _active_criteria_agent is not None:
        try:
            import uuid
            from k8s_autopilot.middleware.goal_criteria import (
                _goal_criteria_request,
                _prompt_with_conversation_context,
                _proposal_from_result,
            )

            req_data: dict[str, Any] = {
                "request_id": str(uuid.uuid4()),
                "objective": objective,
                "kind": "create" if not feedback else "amend",
            }
            if feedback:
                req_data["feedback"] = feedback
            if seed_criteria_str:
                req_data["previous_criteria"] = seed_criteria_str
            if req_data["kind"] == "amend" and not req_data.get("criteria"):
                req_data["criteria"] = seed_criteria_str or objective

            req = _goal_criteria_request(req_data)
            child_input = {
                "messages": [
                    {
                        "role": "user",
                        "content": _prompt_with_conversation_context(req, []),
                    }
                ],
                "criteria_objective": objective,
                "criteria_operation_id": req_data["request_id"],
            }
            result = _active_criteria_agent.invoke(child_input)
            proposal = _proposal_from_result(result)
            if proposal is None and _active_fallback_agent is not None:
                result = _active_fallback_agent.invoke(child_input)
                proposal = _proposal_from_result(result)
            if proposal is not None:
                proposed_obj, crit_md = proposal
                bullets = [
                    line.strip().lstrip("-*•0123456789.) ").strip()
                    for line in crit_md.splitlines()
                    if line.strip().lstrip("-*•0123456789.) ").strip()
                ]
                if bullets:
                    return proposed_obj, bullets
            elif isinstance(result, dict):
                messages = result.get("messages")
                if isinstance(messages, list):
                    for msg in reversed(messages):
                        content = getattr(msg, "content", None) or (
                            msg.get("content") if isinstance(msg, dict) else None
                        )
                        if isinstance(content, str) and content.strip():
                            extracted = [
                                line.strip().lstrip("-*•0123456789.) ").strip()
                                for line in content.splitlines()
                                if line.strip().startswith(("-", "*", "•", "1.", "2.", "3.", "4.", "5."))
                                and line.strip().lstrip("-*•0123456789.) ").strip()
                            ]
                            if extracted:
                                return objective, extracted
        except Exception as exc:
            logger.warning(
                "Criteria agent execution failed in draft_goal_criteria: %s; falling back",
                exc,
            )

    # 2. Try generate_rubric LLM generation (support both patched module-level and generator-level mocks)
    try:
        from unittest.mock import Mock

        mod_gen = globals().get("generate_rubric")
        if isinstance(mod_gen, Mock):
            gen_fn = mod_gen
        elif isinstance(rubric_generator.generate_rubric, Mock):
            gen_fn = rubric_generator.generate_rubric
        else:
            gen_fn = rubric_generator.generate_rubric

        rubric_text = gen_fn(
            objective,
            feedback=feedback,
            previous_criteria=seed_criteria_str,
        )
        bullets = [
            line.strip().lstrip("-*•0123456789.) ").strip()
            for line in rubric_text.splitlines()
            if line.strip().lstrip("-*•0123456789.) ").strip()
        ]
        if bullets:
            return objective, bullets
    except Exception as exc:
        logger.warning("generate_rubric failed in draft_goal_criteria: %s", exc)

    # 3. Fallback to cleaned suggestions if available
    if cleaned_suggestions:
        return objective, cleaned_suggestions

    return objective, [f"Complete objective: {objective}"]


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
                "bullets, or initial suggestions. Concrete criteria will be drafted, refined, and "
                "verified by the criteria agent before presentation to the user."
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
    proposed_obj, criteria_list = draft_goal_criteria(
        objective,
        suggested_criteria=criteria,
    )
    obj_to_use = proposed_obj or objective

    review_request = {
        "type": "goal_review",
        "objective": obj_to_use,
        "criteria": criteria_list,
        "tool_call_id": tool_call_id,
    }
    response = interrupt(review_request)
    return _parse_goal_response(response, obj_to_use, criteria_list, tool_call_id)


