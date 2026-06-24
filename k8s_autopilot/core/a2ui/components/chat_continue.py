"""Chat Continue Summary component."""

from typing import List, Dict, Any

from copilotkit import a2ui

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

        # Pass the LLM's message through as-is.  The model already formats
        # its response with status-appropriate headings (e.g. "❌ Helm
        # Operation Failed"), so adding a generic "✅ Operation Summary"
        # wrapper would be misleading on failure paths.
        markdown_text = f"{message.strip()}\n"

        operations = [
            a2ui.create_surface("chat-continue-summary"),
            a2ui.update_components(
                "chat-continue-summary",
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
                "chat-continue-summary",
                {"markdown": markdown_text},
            ),
        ]
        return operations
