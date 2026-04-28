"""Completion status component."""

from typing import List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=10)
class CompletionComponent(BaseComponent):
    """
    Displays a success card when the agent has completed its task successfully.
    """

    component_type = "completion"

    def can_handle(self, ctx: RenderContext) -> bool:
        return ctx.is_task_complete and ctx.status in ("completed", "success")

    def build(self, ctx: RenderContext) -> List[dict]:
        message = ctx.content_str if ctx.content_str else "Task completed successfully."
        
        markdown_text = (
            f"### ✅ Complete\n\n"
            f"{message.strip()}\n"
        )

        return [
            {
                "beginRendering": {
                    "surfaceId": "completion",
                    "root": "completion-root",
                    "styles": {
                        "primaryColor": "#22C55E",
                        "foregroundColor": "#F8FAFC",
                        "font": "Inter",
                    }
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "completion",
                    "components": [
                        {
                            "id": "completion-root",
                            "component": {
                                "Text": {
                                    "text": {"path": "markdown"},
                                    "usageHint": "body",
                                }
                            }
                        }
                    ]
                }
            },
            {
                "dataModelUpdate": {
                    "surfaceId": "completion",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text}
                    ]
                }
            }
        ]
