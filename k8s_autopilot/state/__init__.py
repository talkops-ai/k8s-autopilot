"""State management module for K8s Autopilot.

Provides agent state schemas, session management, thread persistence, and checkpointers.
"""

from __future__ import annotations

from k8s_autopilot.state.base import AgentState, BaseAgentState, K8sAgentState
from k8s_autopilot.state.goal_channels import (
    GoalProposalKind,
    GoalRubricChannels,
    GoalStatus,
    RUBRIC_RESULT_VALUES,
    coerce_goal_proposal_kind,
    coerce_goal_status,
)
from k8s_autopilot.state.session import (
    SessionManager,
    ThreadInfo,
    create_checkpointer,
    create_runtime_checkpointer,
    delete_thread,
    find_similar_threads,
    format_path,
    format_relative_timestamp,
    format_timestamp,
    generate_thread_id,
    get_active_backend,
    get_cached_threads,
    get_checkpointer,
    get_db_path,
    get_most_recent,
    get_thread_agent,
    get_thread_cwd,
    list_threads,
    populate_thread_checkpoint_details,
    prewarm_thread_message_counts,
    thread_exists,
)

__all__ = [
    "AgentState",
    "BaseAgentState",
    "GoalProposalKind",
    "GoalRubricChannels",
    "GoalStatus",
    "K8sAgentState",
    "RUBRIC_RESULT_VALUES",
    "SessionManager",
    "ThreadInfo",
    "coerce_goal_proposal_kind",
    "coerce_goal_status",
    "create_checkpointer",
    "create_runtime_checkpointer",
    "delete_thread",
    "find_similar_threads",
    "format_path",
    "format_relative_timestamp",
    "format_timestamp",
    "generate_thread_id",
    "get_active_backend",
    "get_cached_threads",
    "get_checkpointer",
    "get_db_path",
    "get_most_recent",
    "get_thread_agent",
    "get_thread_cwd",
    "list_threads",
    "populate_thread_checkpoint_details",
    "prewarm_thread_message_counts",
    "thread_exists",
]
