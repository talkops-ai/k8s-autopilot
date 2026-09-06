"""K8s Autopilot agent state schema.

Matches OpsCode base state with support for message history, goals, and private channels.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class BaseAgentState(TypedDict):
    """Minimal agent state tracking message history with add_messages reducer."""

    messages: Annotated[list[BaseMessage], add_messages]


class K8sAgentState(TypedDict):
    """K8s Autopilot agent state with support for goal and private middleware channels."""

    messages: Annotated[list[BaseMessage], add_messages]
    rubric: NotRequired[str | None]
    _goal_objective: NotRequired[str | None]
    _goal_status: NotRequired[str | None]
    _goal_rubric: NotRequired[str | None]
    _goal_status_note: NotRequired[str | None]
    _pending_goal_completion_note: NotRequired[str | None]
    _sticky_rubric: NotRequired[str | None]
    _auto_decision_plan: NotRequired[Any]
    _auto_temp_artifacts: NotRequired[dict[str, Any]]
    _rubric_status: NotRequired[str | None]
    _resume_state: NotRequired[dict[str, Any] | None]
    _active_plugins: NotRequired[list[str]]
    _active_subagents: NotRequired[dict[str, Any]]


# Standard aliases
AgentState = K8sAgentState
