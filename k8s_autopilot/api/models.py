from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ThreadCreate(BaseModel):
    """POST /threads — request body."""

    thread_id: Optional[UUID] = None
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

    thread_id: UUID
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

    thread_id: UUID
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

    thread_id: UUID
    states: list[dict[str, Any]]
