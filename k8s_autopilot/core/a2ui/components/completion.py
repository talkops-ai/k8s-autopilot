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
        
        return [
            {
                "beginRendering": {
                    "surfaceId": "completion",
                    "root": "completion-root",
                    "styles": {"primaryColor": "#22C55E", "font": "Inter"}
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "completion",
                    "components": [
                        {
                            "id": "completion-root",
                            "component": {"Card": {"child": "completion-content"}}
                        },
                        {
                            "id": "completion-content",
                            "component": {
                                "Column": {
                                    "children": {"explicitList": ["success-header", "divider", "message-text"]}
                                }
                            }
                        },
                        {
                            "id": "success-header",
                            "component": {
                                "Row": {
                                    "children": {"explicitList": ["check-icon", "success-title"]},
                                    "alignment": "center"
                                }
                            }
                        },
                        {
                            "id": "check-icon",
                            "component": {"Icon": {"name": {"literalString": "check"}}}
                        },
                        {
                            "id": "success-title",
                            "component": {
                                "Text": {
                                    "usageHint": "h2",
                                    "text": {"literalString": "✅ Complete"}
                                }
                            }
                        },
                        {"id": "divider", "component": {"Divider": {}}},
                        {
                            "id": "message-text",
                            "component": {
                                "Text": {
                                    "usageHint": "body",
                                    "text": {"path": "message"}
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
                        {"key": "message", "valueString": message}
                    ]
                }
            }
        ]
