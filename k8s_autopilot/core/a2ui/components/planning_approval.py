"""Planning Approval A2UI component.

Intercepts ``request_user_input`` interrupts from the coordinator's
Phase 3 (Plan Approval Gate) and renders them using the polished
``hitlApprovalCard`` surface — the same card used for HITL tool-call
approvals, with the same Approve / Edit / Reject panels.

This unifies the user experience: both planning approval and tool-call
approval use the identical ``hitlApprovalCard`` frontend component.

Priority 9 — above ``UserInputComponent`` (8) so planning approvals
get matched first, while non-planning ``user_input_request`` payloads
(e.g. clarification questions) still fall through to the generic handler.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)
from k8s_autopilot.core.a2ui.surface_builder import build_hitl_approval_surface
from k8s_autopilot.core.a2ui.risk_classification import classify_risk


# Keywords that indicate a planning approval gate
_PLAN_APPROVAL_KEYWORDS = frozenset({
    "approve", "reject", "modify",
    "approve_plan", "reject_plan", "modify_plan",
    "execute", "cancel",
})

# Title keywords that indicate planning context
_PLAN_TITLE_KEYWORDS = frozenset({
    "operation plan",
    "execution plan",
    "plan review",
    "rollback plan",
    "deployment plan",
    "migration plan",
})


@register_component(priority=9)
class PlanningApprovalComponent(BaseComponent):
    """Renders coordinator Phase 3 plan approval as a ``hitlApprovalCard``.

    Matches when:
      - ``require_user_input`` is True
      - ``content.type == "user_input_request"``
      - options contain approve/reject/modify pattern
    """

    component_type = "planning_approval"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False
        if not isinstance(ctx.content, dict):
            return False
        if ctx.content.get("type") != "user_input_request":
            return False

        return self._is_plan_approval(ctx.content)

    def _is_plan_approval(self, content: Dict[str, Any]) -> bool:
        """Detect if a user_input_request is a planning approval gate."""
        options = content.get("options", [])
        if not options:
            return False

        # Check if options contain approve/reject/modify pattern
        option_keys = set()
        for opt in options:
            if isinstance(opt, dict):
                key = opt.get("key", "").lower()
                option_keys.add(key)

        # Must have at least approve + one of reject/modify
        has_approve = bool(option_keys & {"approve", "approve_plan", "execute"})
        has_reject_or_modify = bool(
            option_keys & {"reject", "reject_plan", "modify", "modify_plan", "cancel"}
        )

        if has_approve and has_reject_or_modify:
            return True

        # Also check title for planning keywords
        title = (content.get("title") or "").lower()
        question = (content.get("question") or "").lower()
        combined = f"{title} {question}"

        return any(kw in combined for kw in _PLAN_TITLE_KEYWORDS)

    def build(self, ctx: RenderContext) -> List[dict]:
        content: Dict[str, Any] = ctx.content  # type: ignore[assignment]

        title = content.get("title", "Operation Plan")
        question = content.get("question", "")
        context_text = content.get("context", "")

        # Build the justification text (combines question + context)
        if context_text:
            justification = f"{question}\n\n{context_text}" if question else context_text
        else:
            justification = question or ""

        # Map user_input_request options → hitlApprovalCard options
        raw_options = content.get("options", [])
        hitl_options = self._map_options(raw_options)

        # Classify risk — planning operations are at least "medium"
        phase = self._detect_phase(content)
        risk_level = classify_risk(phase=phase, action_requests=[])

        surface_id = f"plan-approval-{uuid.uuid4().hex[:8]}"

        return build_hitl_approval_surface(
            surface_id=surface_id,
            proposed_action=title,
            justification=justification,
            risk_level=risk_level,
            options=hitl_options,
            action_id="hitl_response",
            phase=phase,
            parameters=[],
        )

    def _map_options(self, raw_options: list) -> list[dict[str, str]]:
        """Map request_user_input options to hitlApprovalCard format."""
        mapped = []
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            key = opt.get("key", "")
            label = opt.get("label", key)

            # Normalize keys to match hitlApprovalCard's expected ids
            if key.lower() in ("approve", "approve_plan", "execute"):
                mapped.append({"id": "approve", "label": label or "✅ Approve and Execute"})
            elif key.lower() in ("modify", "modify_plan", "edit"):
                mapped.append({"id": "edit", "label": label or "✏️ Modify Plan"})
            elif key.lower() in ("reject", "reject_plan", "cancel"):
                mapped.append({"id": "reject", "label": label or "❌ Reject / Cancel"})
            else:
                mapped.append({"id": key, "label": label})

        if not mapped:
            mapped = [
                {"id": "approve", "label": "✅ Approve and Execute"},
                {"id": "edit", "label": "✏️ Modify Plan"},
                {"id": "reject", "label": "❌ Reject / Cancel"},
            ]

        return mapped

    def _detect_phase(self, content: Dict[str, Any]) -> str:
        """Extract a phase name for risk classification."""
        title = (content.get("title") or "").lower()
        if "rollback" in title:
            return "rollback_plan_review"
        if "deploy" in title:
            return "deployment_plan_review"
        if "migration" in title:
            return "migration_plan_review"
        if "delete" in title or "uninstall" in title:
            return "deletion_plan_review"
        return "operation_plan_review"
