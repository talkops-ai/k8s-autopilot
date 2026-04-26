"""Working status component."""

from typing import List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=0)
class WorkingStatusComponent(BaseComponent):
    """
    Displays a status card while the agent is processing.
    """

    component_type = "working_status"

    def can_handle(self, ctx: RenderContext) -> bool:
        return (
            ctx.status == "working"
            and not ctx.is_task_complete
            and not ctx.require_user_input
            and ctx.response_type not in ("error", "a2ui")
        )

    def build(self, ctx: RenderContext) -> List[dict]:
        status_text = ctx.status.replace('_', ' ').title()
        content = ctx.content_str if ctx.content_str else "Processing..."
        
        markdown_text = (
            f"### ⏳ {status_text}\n\n"
            f"{content.strip()}\n"
        )

        return [
            {
                "beginRendering": {
                    "surfaceId": "status",
                    "root": "status-root",
                    "styles": {
                        "primaryColor": "#818cf8",
                        "foregroundColor": "#E2E8F0",
                        "font": "Inter",
                    }
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "status",
                    "components": [
                        {
                            "id": "status-root",
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
                    "surfaceId": "status",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text}
                    ]
                }
            }
        ]
