"""Info Message component."""

import json
from typing import List, Any, Dict

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=3)
class InfoMessageComponent(BaseComponent):
    """
    Displays an informational message card.
    
    Used when the agent is providing information, asking for clarification,
    or explaining its capabilities - NOT for approval requests.
    """

    component_type = "info_message"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False

        if ctx.phase == "values_confirmation":
            return False

        # Let UserInputComponent handle user_input_request payloads
        if (
            isinstance(ctx.content, dict)
            and ctx.content.get("type") == "user_input_request"
        ):
            return False

        # If it's NOT an approval request, we consider it an info message
        return not self._is_approval_request(ctx.content, ctx.metadata)

    def _is_approval_request(self, content: Any, metadata: Dict[str, Any]) -> bool:
        """Keep this synchronized with HitlApprovalComponent's detection logic."""
        interrupt_type = metadata.get('interrupt_type', '')
        if interrupt_type in ('hitl_gate', 'planning_review', 'generation_review', 'tool_result_review', 'critical_tool_call_approval'):
            return True
            
        if isinstance(content, dict) and content.get('type') == 'tool_call_approval_request':
            return True
            
        return False

    def build(self, ctx: RenderContext) -> List[dict]:
        target_content = ctx.content
        content_str = str(target_content) if target_content else "Awaiting your input..."

        if isinstance(target_content, dict):
            # Extract questions/messages from generic dicts
            message = target_content.get('summary', target_content.get('question', target_content.get('message', content_str)))
        else:
            message = str(target_content)

        title = ctx.metadata.get("title", "")

        # Render as clean markdown — no card/oval shape, no icon, no divider.
        # Each line of the question is rendered as readable markdown text so
        # the client displays it inline with the conversation.
        markdown_text = f"### {title}\n\n{message.strip()}\n" if title else f"{message.strip()}\n"

        return [
            {
                "beginRendering": {
                    "surfaceId": "info-message",
                    "root": "info-root",
                    "styles": {
                        "primaryColor": "#818cf8",
                        "foregroundColor": "#E2E8F0",
                        "font": "Inter",
                    },
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "info-message",
                    "components": [
                        {
                            "id": "info-root",
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
                    "surfaceId": "info-message",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text},
                    ],
                }
            },
        ]
