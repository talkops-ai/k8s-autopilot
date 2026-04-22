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
        
        return [
            {
                "beginRendering": {
                    "surfaceId": "status",
                    "root": "status-root",
                    "styles": {"primaryColor": "#818cf8", "font": "Inter"}
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "status",
                    "components": [
                        {
                            "id": "status-root",
                            "component": {
                                "Card": {"child": "status-content"}
                            }
                        },
                        {
                            "id": "status-content",
                            "component": {
                                "Column": {
                                    "children": {"explicitList": ["status-header", "status-text"]}
                                }
                            }
                        },
                        {
                            "id": "status-header",
                            "component": {
                                "Row": {
                                    "children": {"explicitList": ["status-icon", "status-title"]},
                                    "alignment": "center"
                                }
                            }
                        },
                        {
                            "id": "status-icon",
                            "component": {
                                "Icon": {"name": {"literalString": "settings"}}
                            }
                        },
                        {
                            "id": "status-title",
                            "component": {
                                "Text": {
                                    "usageHint": "h4",
                                    "text": {"path": "title"}
                                }
                            }
                        },
                        {
                            "id": "status-text",
                            "component": {
                                "Text": {
                                    "usageHint": "body",
                                    "text": {"path": "content"}
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
                        {"key": "title", "valueString": f"⏳ {status_text}"},
                        {"key": "content", "valueString": ctx.content_str}
                    ]
                }
            }
        ]
