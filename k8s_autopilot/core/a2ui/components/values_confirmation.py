"""Values Confirmation component."""

import json
from typing import List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=30)
class ValuesConfirmationComponent(BaseComponent):
    """
    Displays a values confirmation card with an Accept Defaults button.
    """

    component_type = "values_confirmation"

    def can_handle(self, ctx: RenderContext) -> bool:
        return ctx.require_user_input and ctx.phase == "values_confirmation"

    def build(self, ctx: RenderContext) -> List[dict]:
        target_content = ctx.content
        content_str = str(target_content) if target_content else "Please confirm values."

        if isinstance(target_content, dict):
            # Try to extract a question from the content
            question = target_content.get('summary', target_content.get('question', target_content.get('message', content_str)))
            # Extract additional context 
            context_text = target_content.get('context', '')
            if isinstance(context_text, dict):
                context_text = json.dumps(context_text, indent=2)
            else:
                context_text = str(context_text)
        else:
            question = str(target_content)
            context_text = ''
            
        context_display = context_text if context_text else "Please refer to content above"

        return [
            {
                "beginRendering": {
                    "surfaceId": "values-form",
                    "root": "values-root",
                    "styles": {"primaryColor": "#818cf8", "font": "Inter"}
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "values-form",
                    "components": [
                        {
                            "id": "values-root",
                            "component": {"Card": {"child": "values-content"}}
                        },
                        {
                            "id": "values-content",
                            "component": {
                                "Column": {
                                    "children": {
                                        "explicitList": [
                                            "values-header",
                                            "divider1",
                                            "question-text",
                                            "context-text",
                                            "divider2",
                                            "action-row"
                                        ]
                                    }
                                }
                            }
                        },
                        {
                            "id": "values-header",
                            "component": {
                                "Row": {
                                    "children": {"explicitList": ["header-icon", "header-title"]},
                                    "alignment": "center"
                                }
                            }
                        },
                        {
                            "id": "header-icon",
                            "component": {"Icon": {"name": {"literalString": "settings"}}}
                        },
                        {
                            "id": "header-title",
                            "component": {
                                "Text": {
                                    "usageHint": "h3",
                                    "text": {"literalString": "Values Confirmation"}
                                }
                            }
                        },
                        {"id": "divider1", "component": {"Divider": {}}},
                        {
                            "id": "question-text",
                            "component": {
                                "Text": {
                                    "usageHint": "body",
                                    "text": {"path": "question"}
                                }
                            }
                        },
                        {
                             "id": "context-text",
                             "component": {
                                 "Text": {
                                     "usageHint": "caption",
                                     "text": {"path": "context"}
                                 }
                             }
                        },
                        {"id": "divider2", "component": {"Divider": {}}},
                        {
                            "id": "action-row",
                            "component": {
                                "Row": {
                                    "children": {"explicitList": ["accept-btn"]},
                                    "distribution": "end"
                                }
                            }
                        },
                        {
                            "id": "accept-btn",
                            "component": {
                                "Button": {
                                    "child": "accept-text",
                                    "primary": True,
                                    "action": {
                                        "name": "hitl_response",
                                        "context": [
                                            {"key": "decision", "value": {"literalString": "accept_defaults"}},
                                            {"key": "phase", "value": {"literalString": "values_confirmation"}}
                                        ]
                                    }
                                }
                            }
                        },
                        {
                            "id": "accept-text",
                            "component": {
                                "Text": {"text": {"literalString": "✅ Accept Defaults"}}
                            }
                        }
                    ]
                }
            },
            {
                "dataModelUpdate": {
                    "surfaceId": "values-form",
                    "path": "/",
                    "contents": [
                        {"key": "question", "valueString": question},
                        {"key": "context", "valueString": context_display}
                    ]
                }
            }
        ]
