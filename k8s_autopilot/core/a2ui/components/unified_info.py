"""Unified Info/Status component — single A2UI Text surface for all non-interactive messages.

Replaces four separate components:
  - ``WorkingStatusComponent``   (⏳ Processing)
  - ``CompletionComponent``      (✅ Complete)
  - ``ErrorComponent``           (❌ Error)
  - ``InfoMessageComponent``     (ℹ️ Info)

All render the same A2UI ``Text`` surface with markdown.
The only differences are the icon prefix and surface ID.
"""

from __future__ import annotations

from typing import Any, Dict, List

from copilotkit import a2ui

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


# ---------------------------------------------------------------------------
# Status → (icon, heading, surface_id)
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, tuple[str, str, str]] = {
    "working":   ("⏳", "Processing",  "status"),
    "completed": ("✅", "Complete",     "completion"),
    "success":   ("✅", "Complete",     "completion"),
    "error":     ("❌", "Error",        "error"),
    "failed":    ("❌", "Error",        "error"),
}


@register_component(priority=3)
class UnifiedInfoComponent(BaseComponent):
    """Renders a markdown text surface for status messages and info prompts.

    Matches when:
      - Working status (processing, no user input)
      - Completion (task done, success)
      - Error (task failed)
      - Informational messages (require user input but NOT an approval)

    Priority 3 — lowest priority among content components, acts as the
    final fallback for non-approval interrupts.
    """

    component_type = "unified_info"

    def can_handle(self, ctx: RenderContext) -> bool:
        # ── Error / failure states ────────────────────────────────
        if ctx.status in ("failed", "error") or ctx.response_type == "error":
            return True

        # ── Completion states ─────────────────────────────────────
        if ctx.is_task_complete and ctx.status in ("completed", "success"):
            return True

        # ── Working status (no user input) ────────────────────────
        if (
            ctx.status == "working"
            and not ctx.is_task_complete
            and not ctx.require_user_input
            and ctx.response_type not in ("error", "a2ui")
        ):
            return True

        # ── Info messages (require input but NOT approval) ────────
        if ctx.require_user_input:
            # Don't match values_confirmation or user_input_request
            if ctx.phase == "values_confirmation":
                return False
            if (
                isinstance(ctx.content, dict)
                and ctx.content.get("type") in ("user_input_request", "chat_continue")
            ):
                return False
            # Don't match approval requests
            if self._is_approval_request(ctx.content, ctx.metadata):
                return False
            return True

        return False

    def _is_approval_request(self, content: Any, metadata: Dict[str, Any]) -> bool:
        """Keep this synchronized with UnifiedApprovalComponent's detection."""
        interrupt_type = metadata.get("interrupt_type", "")
        if interrupt_type in (
            "hitl_gate", "planning_review", "generation_review",
            "tool_result_review", "critical_tool_call_approval",
            "hitl_approval",
        ):
            return True

        if isinstance(content, dict) and content.get("type") in (
            "tool_call_approval_request", "hitl_approval",
        ):
            return True

        return False

    def build(self, ctx: RenderContext) -> List[dict]:
        # ── Determine icon + heading + surface ID ─────────────────
        status_key = ctx.status or ""
        if ctx.response_type == "error":
            status_key = "error"

        icon, heading, surface_id = _STATUS_MAP.get(
            status_key,
            ("ℹ️", "Information", "info-message"),
        )

        # ── Build markdown text ───────────────────────────────────
        if ctx.require_user_input:
            # Info message path
            target_content = ctx.content
            if isinstance(target_content, dict):
                message = str(
                    target_content.get("summary")
                    or target_content.get("question")
                    or target_content.get("message")
                    or str(target_content)
                )
            else:
                message = str(target_content) if target_content else "Awaiting your input..."

            title = str(ctx.metadata.get("title", ""))
            if title:
                markdown_text = f"### {title}\n\n{message.strip()}\n"
            else:
                markdown_text = f"{message.strip()}\n"
        else:
            content = ctx.content_str if ctx.content_str else (
                "Task completed successfully." if status_key in ("completed", "success")
                else "Processing..." if status_key == "working"
                else "An unknown error occurred." if status_key in ("error", "failed")
                else "Processing..."
            )

            if status_key in ("error", "failed"):
                markdown_text = f"### {icon} {heading}\n\n**{content.strip()}**\n"
            else:
                markdown_text = f"### {icon} {heading}\n\n{content.strip()}\n"

        # ── Build A2UI operations ─────────────────────────────────
        return [
            a2ui.create_surface(surface_id),
            a2ui.update_components(
                surface_id,
                [
                    {
                        "id": "root",
                        "component": {
                            "Text": {
                                "text": {"path": "markdown"},
                                "usageHint": "body",
                            }
                        },
                    },
                ],
            ),
            a2ui.update_data_model(
                surface_id,
                {"markdown": markdown_text},
            ),
        ]
