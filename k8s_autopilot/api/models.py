from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ThreadCreate(BaseModel):
    """POST /threads — request body."""

    thread_id: Optional[UUID | str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    title: str = ""


class ThreadUpdate(BaseModel):
    """PATCH /threads/{thread_id} — request body."""

    title: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ThreadSearch(BaseModel):
    """POST /threads/search — request body."""

    user_id: str = "default"
    agent_id: Optional[str] = None
    status: Optional[str] = None
    limit: int = 20
    offset: int = 0
    sort_by: str = "updated_at"
    sort_order: str = "desc"


class ThreadResponse(BaseModel):
    """Standard thread metadata response."""

    thread_id: UUID | str
    title: str
    status: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadStateResponse(BaseModel):
    """GET /threads/{thread_id}/state — response.

    Exposes the full supervisor state contract for frontend consumption.
    Phase 1: routing_decision, ui_payload, conversation_summary
    Phase 3: interrupts (persisted HITL payloads for history replay)
    """

    thread_id: UUID | str
    title: str
    status: str
    messages: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    # ── Phase 1: Clean state contract ─────────────────────────────
    routing_decision: dict[str, Any] | None = None
    ui_payload: dict[str, Any] | None = None
    conversation_summary: str | None = None

    # ── Phase 3: HITL interrupt payloads for replay ───────────────
    interrupts: list[dict[str, Any]] = Field(default_factory=list)


class ThreadHistoryResponse(BaseModel):
    """GET /threads/{thread_id}/history — response."""

    thread_id: UUID | str
    states: list[dict[str, Any]]


# ── Status Bar & Telemetry Models ─────────────────────────────────────────

class GoalTelemetry(BaseModel):
    """Goal & Rubric telemetry state."""

    objective: Optional[str] = None
    status: Optional[str] = None
    rubric_label: Optional[str] = None
    rubric: Optional[str] = None


class UsageTelemetry(BaseModel):
    """Token and cost usage metrics."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class ModelTelemetry(BaseModel):
    """Active model and reasoning profile."""

    spec: str
    provider: str
    name: str
    reasoning_effort: str = "medium"


class SubagentTelemetry(BaseModel):
    """Registered subagent status."""

    name: str
    status: str = "idle"


class ThreadTelemetryResponse(BaseModel):
    """GET /threads/{thread_id}/telemetry — response."""

    thread_id: UUID | str
    approval_mode: str = "manual"
    goal: Optional[GoalTelemetry] = None
    usage: UsageTelemetry = Field(default_factory=UsageTelemetry)
    model: ModelTelemetry
    subagents: list[SubagentTelemetry] = Field(default_factory=list)


class ApprovalModeUpdateRequest(BaseModel):
    """POST /api/settings/approval-mode — request body."""

    mode: str
    thread_id: Optional[str] = None


class ApprovalModeResponse(BaseModel):
    """POST /api/settings/approval-mode — response."""

    status: str = "success"
    approval_mode: str
