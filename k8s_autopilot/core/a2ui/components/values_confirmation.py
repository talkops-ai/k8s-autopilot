"""Values Confirmation component."""

import json
from typing import List

from copilotkit import a2ui

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
        
        markdown_text = (
            f"### ⚙️ Values Confirmation\n\n"
            f"**{question}**\n\n"
            f"{context_display}\n"
        )

        components = [
            {
                "id": "root",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "markdown-text",
                                "divider",
                                "action-row"
                            ]
                        }
                    }
                }
            },
            {
                "id": "markdown-text",
                "component": {
                    "Text": {
                        "usageHint": "body",
                        "text": {"path": "markdown"}
                    }
                }
            },
            {"id": "divider", "component": {"Divider": {}}},
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

        operations = [
            a2ui.create_surface("values-form"),
            a2ui.update_components("values-form", components),
            a2ui.update_data_model(
                "values-form",
                {"markdown": markdown_text},
            ),
        ]
        return operations
