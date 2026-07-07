"""Typed outward-facing result from a coordinator deep agent.

Every coordinator's ``output_transform()`` returns a dict conforming to
this model.  The supervisor's ``_make_coordinator_node`` reads these
fields to:

1. Persist a clean ``AIMessage(content=summary_text)`` in the
   supervisor's ``messages`` channel (user-facing conversation ledger).
2. Store structured UI data in ``ui_payload`` for frontend replay.
3. Pass domain summaries to the cross-domain blackboard.
4. Detect cross-domain handoff requests.

This replaces the ad-hoc ``{"final_message": ..., "status": ...}`` dict
pattern that mixed user-facing text with internal coordinator output.

Reference: deep-agent-architecture-review.md §Recommended Target Architecture
    — "Coordinators return curated outputs"
    — "Each coordinator should own an explicit outward-facing result model"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CoordinatorResult(BaseModel):
    """Typed outward-facing result from a coordinator.

    Replaces the ad-hoc dict with explicit fields so the supervisor
    and frontend have a stable, typed contract.

    Fields:
        summary_text:    Concise human-readable summary appended to
                         ``messages[]`` as ``AIMessage.content``.
        ui_payload:      Structured data for rich UI replay (tables,
                         cards, approval forms).  Persisted in the
                         ``ui_payload`` state key so history replay
                         can reconstruct the original surface without
                         ``output_transform`` flat-text conversion.
        artifacts:       Domain-specific outputs (files, charts,
                         synced paths, structured responses).
        domain_summary:  Compact dict for the cross-domain blackboard.
        status:          Completion status string.
        handoff_request: Cross-domain handoff envelope (if applicable).
    """

    summary_text: str = Field(
        description=(
            "Concise human-readable summary for the conversation ledger. "
            "This becomes the AIMessage.content in the supervisor's messages[]."
        ),
    )
    ui_payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Structured UI data for frontend replay.  Contains typed "
            "payloads (table schema, card data, approval form) that the "
            "frontend renders during both live streaming and history replay."
        ),
    )
    artifacts: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Domain-specific outputs: files, synced paths, structured "
            "responses, and any other coordinator-internal data."
        ),
    )
    domain_summary: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Compact summary for the cross-domain blackboard.  Accumulated "
            "by the supervisor and injected into downstream coordinators "
            "for cross-domain awareness."
        ),
    )
    status: str = Field(
        default="completed",
        description="Completion status: 'completed', 'error', 'escalated', etc.",
    )
    handoff_request: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Cross-domain handoff envelope.  When present, the supervisor "
            "routes to the target coordinator instead of completing."
        ),
    )
