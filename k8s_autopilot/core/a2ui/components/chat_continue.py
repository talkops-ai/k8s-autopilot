"""Chat Continue Summary component."""

from typing import List, Dict, Any

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=5)
class ChatContinueComponent(BaseComponent):
    """
    Displays a rich, markdown-formatted Operation Summary when the agent
    pauses execution to present results before waiting for the next command.

    Renders as clean inline markdown (no rigid card layout) so the client
    can display headings, tables, bold text, and bullet points natively —
    producing a polished, Claude-Desktop-like reading experience.
    """

    component_type = "chat_continue"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False

        if isinstance(ctx.content, dict) and ctx.content.get("type") == "chat_continue":
            return True

        return False

    def build(self, ctx: RenderContext) -> List[dict]:
        content: Dict[str, Any] = ctx.content  # type: ignore[assignment]
        message = content.get("question", "Operation completed. Waiting for instructions...")

        # Build rich markdown: the LLM's message is already markdown-formatted
        # via the coordinator prompt. We wrap it with a styled header.
        markdown_text = (
            f"### ✅ Operation Summary\n\n"
            f"{message.strip()}\n"
        )

        return [
            {
                "beginRendering": {
                    "surfaceId": "chat-continue-summary",
                    "root": "chat-cont-root",
                    "styles": {
                        "primaryColor": "#10B981",
                        "foregroundColor": "#F8FAFC",
                        "font": "Inter",
                    },
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "chat-continue-summary",
                    "components": [
                        {
                            "id": "chat-cont-root",
                            "component": {
                                "Text": {
                                    "text": {"path": "markdown"},
                                    "usageHint": "body",
                                }
                            },
                        },
                    ],
                }
            },
            {
                "dataModelUpdate": {
                    "surfaceId": "chat-continue-summary",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text},
                    ],
                }
            },
        ]
