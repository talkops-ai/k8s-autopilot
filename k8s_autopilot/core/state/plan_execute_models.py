"""
Plan-and-Execute Execution Contract Models.

Defines the structured data models for the enhanced planning mode:

    TodoItem        — Single planned task in an execution plan
    ExecutionEvent  — Structured telemetry from a completed task
    PlanEnvelope    — Top-level plan wrapper with versioning and approval

These models align with LangChain Deep Agent's built-in ``write_todos`` tool
and ``TodoListMiddleware``, extending the native ``pending → in_progress →
completed`` lifecycle with execution telemetry, dependency tracking, and
walkthrough synthesis.

Reference: k8s_autopilot_plan_execute_architecture.md §State model
Docs: https://docs.langchain.com/oss/python/deepagents/frontend/todo-list
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TodoStatus(str, Enum):
    """Status lifecycle for a planned task.

    Matches Deep Agent's built-in ``write_todos`` status values so that
    ``stream.values.todos`` renders correctly in the frontend.
    """
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalState(str, Enum):
    """Approval lifecycle for a plan envelope."""
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    ABORTED = "aborted"


class TaskCategory(str, Enum):
    """Standard task categories for Kubernetes operations.

    Domain-specific coordinators may use a subset of these.
    """
    DISCOVERY = "discovery"
    MANIFEST_GENERATION = "manifest_generation"
    VALIDATION = "validation"
    DRY_RUN = "dry_run"
    LIVE_APPLY = "live_apply"
    HEALTH_CHECK = "health_check"
    ROLLOUT = "rollout"
    ROLLBACK = "rollback"
    SUMMARY = "summary"
    CONFIGURATION = "configuration"
    MONITORING = "monitoring"


class ExecutionMode(str, Enum):
    """Whether a task executed as live, dry-run, or read-only."""
    LIVE = "live"
    DRY_RUN = "dry_run"
    READ_ONLY = "read_only"


class ExecutionStatus(str, Enum):
    """Outcome status for an execution event."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


# ---------------------------------------------------------------------------
# TodoItem — Single planned task
# ---------------------------------------------------------------------------

class TodoItem(BaseModel):
    """A single planned task in the execution plan.

    Designed to be compatible with Deep Agent's ``write_todos`` tool output.
    The ``id``, ``title``, ``status``, and ``content`` fields match the
    native schema so that ``stream.values.todos`` works out of the box.

    Additional fields (``category``, ``subagent_type``, ``dependencies``, etc.)
    provide the execution contract metadata needed for deterministic routing,
    dependency-aware sequencing, and walkthrough generation.
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4())[:8],
        description="Stable short ID for tracking and dependency references",
    )
    title: str = Field(
        ...,
        description="Human-readable title (shown in TODO list UI)",
    )
    content: str = Field(
        default="",
        description="Detailed description of the task (Deep Agent native field)",
    )
    status: TodoStatus = Field(
        default=TodoStatus.PENDING,
        description="Current lifecycle status",
    )

    # ── Execution contract extensions ─────────────────────────────────
    category: TaskCategory = Field(
        default=TaskCategory.DISCOVERY,
        description="Task category for routing and audit",
    )
    subagent_type: str = Field(
        default="",
        description="Target sub-agent name (e.g., argocd-onboarder, k8s-cluster-ops)",
    )
    cluster_scope: Optional[str] = Field(
        default=None,
        description="Namespace/cluster restriction for this task",
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="IDs of prerequisite tasks that must complete first",
    )
    requires_live_mutation: bool = Field(
        default=False,
        description="Safety flag — true if this task modifies cluster state",
    )
    human_summary: Optional[str] = Field(
        default=None,
        description="Post-execution summary for walkthrough generation",
    )
    artifacts: Dict[str, Any] = Field(
        default_factory=dict,
        description="Paths or references to outputs (diffs, manifests, logs)",
    )

    def to_write_todos_format(self) -> Dict[str, Any]:
        """Convert to the format expected by Deep Agent's write_todos tool.

        Returns a dict with ``title``, ``status``, and ``content`` keys
        that TodoListMiddleware can consume and stream to the frontend.
        """
        return {
            "title": self.title,
            "status": self.status.value,
            "content": self.content or self.title,
        }


# ---------------------------------------------------------------------------
# ExecutionEvent — Structured telemetry from a completed task
# ---------------------------------------------------------------------------

class ExecutionEvent(BaseModel):
    """Structured telemetry from a completed or failed task.

    Each worker sub-agent returns one of these upon completion. The collection
    of ExecutionEvents forms the execution ledger that the walkthrough
    generator consumes to produce an operator-facing narrative.

    Reference: k8s_autopilot_plan_execute_architecture.md §Execution contract objects
    """
    todo_id: str = Field(
        ..., description="The TodoItem.id this event corresponds to",
    )
    subagent_type: str = Field(
        ..., description="Which sub-agent executed this task",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When execution began (UTC)",
    )
    ended_at: Optional[datetime] = Field(
        default=None,
        description="When execution completed (UTC)",
    )
    status: ExecutionStatus = Field(
        ..., description="Outcome of the execution",
    )
    execution_mode: ExecutionMode = Field(
        default=ExecutionMode.READ_ONLY,
        description="Whether the task ran as live, dry-run, or read-only",
    )
    resource_refs: List[str] = Field(
        default_factory=list,
        description="Kubernetes resource references affected (e.g., 'Deployment/frontend')",
    )
    tool_actions: List[str] = Field(
        default_factory=list,
        description="MCP tool calls made during execution",
    )
    observations: str = Field(
        default="",
        description="Key observations from the execution",
    )
    summary: str = Field(
        default="",
        description="Human-readable summary of what happened",
    )
    artifact_refs: Dict[str, str] = Field(
        default_factory=dict,
        description="Named references to produced artifacts",
    )
    error_details: Optional[str] = Field(
        default=None,
        description="Error message and stack trace if status is FAILED",
    )

    def duration_seconds(self) -> Optional[float]:
        """Calculate execution duration in seconds."""
        if self.ended_at and self.started_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


# ---------------------------------------------------------------------------
# PlanEnvelope — Top-level plan wrapper
# ---------------------------------------------------------------------------

class PlanEnvelope(BaseModel):
    """Top-level wrapper for a plan with versioning and approval state.

    This is the structured plan object that gets serialized into
    ``MainSupervisorState.plan_envelope`` for cross-coordinator awareness
    and persisted via checkpoints for HITL resume.

    The ``plan_version`` field increments on each replan cycle (max 3 by default),
    enabling the rejection protocol: reject → feedback → replan → re-approve.
    """
    plan_version: int = Field(
        default=1,
        description="Incremented on each replan cycle",
    )
    max_replan_attempts: int = Field(
        default=3,
        description="Maximum allowed replan cycles before asking user to rephrase",
    )
    approval_state: ApprovalState = Field(
        default=ApprovalState.DRAFT,
        description="Current approval lifecycle state",
    )
    todos: List[TodoItem] = Field(
        default_factory=list,
        description="The structured plan as a list of TodoItems",
    )
    pending_steps: List[str] = Field(
        default_factory=list,
        description="TodoItem IDs not yet dispatched",
    )
    running_steps: List[str] = Field(
        default_factory=list,
        description="TodoItem IDs currently being executed",
    )
    executed_steps: List[ExecutionEvent] = Field(
        default_factory=list,
        description="Append-only ledger of completed or failed work",
    )
    walkthrough: Optional[str] = Field(
        default=None,
        description="Final synthesized walkthrough narrative",
    )
    user_intent: Optional[str] = Field(
        default=None,
        description="Original user request in plain English",
    )
    coordinator: Optional[str] = Field(
        default=None,
        description="Which coordinator owns this plan",
    )

    # ── Lifecycle helpers ─────────────────────────────────────────────

    def get_next_pending(self) -> Optional[TodoItem]:
        """Return the next pending task whose dependencies are satisfied."""
        completed_ids = {
            e.todo_id for e in self.executed_steps
            if e.status == ExecutionStatus.SUCCESS
        }
        for todo in self.todos:
            if todo.status == TodoStatus.PENDING:
                if all(dep in completed_ids for dep in todo.dependencies):
                    return todo
        return None

    def mark_in_progress(self, todo_id: str) -> None:
        """Mark a task as in-progress and move it to running_steps."""
        for todo in self.todos:
            if todo.id == todo_id:
                todo.status = TodoStatus.IN_PROGRESS
                break
        if todo_id in self.pending_steps:
            self.pending_steps.remove(todo_id)
        if todo_id not in self.running_steps:
            self.running_steps.append(todo_id)

    def record_completion(self, event: ExecutionEvent) -> None:
        """Record a completed execution event and update task status."""
        for todo in self.todos:
            if todo.id == event.todo_id:
                todo.status = (
                    TodoStatus.COMPLETED
                    if event.status == ExecutionStatus.SUCCESS
                    else TodoStatus.FAILED
                )
                todo.human_summary = event.summary
                break
        if event.todo_id in self.running_steps:
            self.running_steps.remove(event.todo_id)
        self.executed_steps.append(event)

    def can_replan(self) -> bool:
        """Check if another replan attempt is allowed."""
        return self.plan_version < self.max_replan_attempts

    def increment_version(self) -> None:
        """Increment plan version for a replan cycle."""
        self.plan_version += 1
        self.approval_state = ApprovalState.DRAFT

    def to_write_todos_list(self) -> List[Dict[str, Any]]:
        """Convert all todos to the format for Deep Agent's write_todos.

        This is the payload to pass to ``write_todos(todos)``.
        """
        return [todo.to_write_todos_format() for todo in self.todos]

    @property
    def progress_summary(self) -> str:
        """Return a one-line progress summary like '3/5 tasks completed (60%)'."""
        total = len(self.todos)
        if total == 0:
            return "No tasks planned"
        completed = sum(1 for t in self.todos if t.status == TodoStatus.COMPLETED)
        pct = round((completed / total) * 100)
        return f"{completed}/{total} tasks completed ({pct}%)"
