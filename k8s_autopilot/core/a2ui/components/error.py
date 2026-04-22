"""Error status component."""

from typing import List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=10)
class ErrorComponent(BaseComponent):
    """
    Displays an error card when a task fails.
    """

    component_type = "error"

    def can_handle(self, ctx: RenderContext) -> bool:
        return ctx.status in ("failed", "error") or ctx.response_type == "error"

    def build(self, ctx: RenderContext) -> List[dict]:
        error_message = ctx.content_str if ctx.content_str else "An unknown error occurred."
        markdown_text = f"### ❌ Error\n\n**{error_message}**\n"
        
        return [
            {
                "beginRendering": {
                    "surfaceId": "error",
                    "root": "error-root",
                    "styles": {
                        "primaryColor": "#EF4444",
                        "foregroundColor": "#E2E8F0",
                        "font": "Inter",
                    }
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "error",
                    "components": [
                        {
                            "id": "error-root",
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
                    "surfaceId": "error",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text},
                    ],
                }
            },
        ]
