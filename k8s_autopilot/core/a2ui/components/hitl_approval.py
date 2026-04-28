"""HITL Approval component."""

from typing import Any, Dict, List
import json

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


@register_component(priority=20)
class HitlApprovalComponent(BaseComponent):
    """
    Displays a HITL approval card with Approve/Reject buttons.
    """

    component_type = "hitl_approval"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False
            
        # Check if it's a values confirmation (handled by another component)
        if ctx.phase == "values_confirmation":
            return False
            
        return self._is_approval_request(ctx.content, ctx.metadata)

    def _is_approval_request(self, content: Any, metadata: Dict[str, Any]) -> bool:
        """Detect if the HITL request is an approval request or just informational."""
        interrupt_type = metadata.get('interrupt_type', '')
        if interrupt_type in ('hitl_gate', 'planning_review', 'generation_review', 'tool_result_review', 'critical_tool_call_approval', 'hitl_approval'):
            return True
            
        if isinstance(content, dict) and content.get('type') in ('tool_call_approval_request', 'hitl_approval'):
            return True
            
        content_str = str(content).lower() if content else ""
        info_keywords = [
            'i specialize in', 'how can i help', 'i am designed for',
            'my capabilities', 'i can help with', 'what would you like',
            'please provide', 'could you clarify', 'more information'
        ]
        
        for keyword in info_keywords:
            if keyword in content_str:
                return False
                
        # Detect payload from `request_human_input`
        if isinstance(content, dict) and "question" in content and "phase" in content:
            if content.get("phase") not in ["unknown", "generic"]:
                return True
        
        # Detect `request_human_input` payload that has been wrapped by generic_interrupt
        if isinstance(content, dict) and content.get("type") == "generic_interrupt":
            data = content.get("data", {})
            if isinstance(data, dict) and "question" in data and "phase" in data:
                if data.get("phase") not in ["unknown", "generic"]:
                    return True

        return False

    # Mapping of tool names → (emoji, human label)
    _TOOL_LABELS: dict = {
        "helm_install_chart": ("🚀", "Install"),
        "helm_upgrade_release": ("⬆️", "Upgrade"),
        "helm_rollback_release": ("⏪", "Rollback"),
        "helm_uninstall_release": ("🗑️", "Uninstall"),
        # ArgoCD App tools
        "create_application": ("➕", "Create App"),
        "sync_application": ("🔄", "Sync App"),
        "delete_application": ("🗑️", "Delete App"),
        "update_application": ("✏️", "Update App"),
        "rollback_application": ("⏪", "Rollback App"),
        "rollback_to_revision": ("⏪", "Rollback Revision"),
        "hard_refresh": ("♻️", "Hard Refresh"),
        # Argo Rollouts tools
        "argo_rollouts_promote": ("🟢", "Promote Rollout"),
        "argo_rollouts_abort": ("🛑", "Abort Rollout"),
        "argo_rollouts_retry": ("🔁", "Retry Rollout"),
    }

    def _format_action_requests_for_ui(
        self, action_requests: list,
    ) -> str:
        """Build a human-readable summary from HITL action_requests.

        Groups actions by tool name and formats each with release name
        and namespace for clear presentation on the approval card.

        Example output::

            🗑️ Uninstall (7 releases):
              • argo-cd → namespace: argocd
              • ingress-nginx → namespace: mgmt
              • traefik → namespace: traefik
        """
        # Group by tool name, preserving insertion order
        groups: Dict[str, list] = {}
        for req in action_requests:
            if not isinstance(req, dict):
                continue
            name = req.get("name", "unknown")
            args = req.get("args", {})
            groups.setdefault(name, []).append(args)

        lines: List[str] = []
        for tool_name, arg_list in groups.items():
            emoji, label = self._TOOL_LABELS.get(
                tool_name,
                ("⚙️", tool_name.replace("_", " ").title()),
            )
            count = len(arg_list)
            
            # Determine dynamic resource label
            if "application" in tool_name or "argo" in tool_name and "rollouts" not in tool_name:
                item_label = "app"
            elif "rollouts" in tool_name:
                item_label = "rollout"
            elif "helm" in tool_name:
                item_label = "release"
            elif "traefik" in tool_name:
                item_label = "route"
            else:
                item_label = "action"
                
            plural = "s" if count != 1 else ""
            lines.append(
                f"{emoji} {label} ({count} {item_label}{plural}):",
            )
            for args in arg_list:
                release = args.get(
                    "release_name",
                    args.get("chart_name",
                    args.get("app_name",
                    args.get("rollout_name",
                    args.get("name", "unknown"))))
                )
                ns = args.get("namespace", "default")
                extras: List[str] = []
                if "version" in args:
                    extras.append(f"v{args['version']}")
                if "revision" in args:
                    extras.append(f"rev {args['revision']}")
                suffix = f" ({', '.join(extras)})" if extras else ""
                lines.append(
                    f"  • {release} → namespace: {ns}{suffix}",
                )
            lines.append("")  # spacing between groups

        return "\n".join(lines).strip() or "Action requires approval."

    def build(self, ctx: RenderContext) -> List[dict]:
        target_content = ctx.content
        content_str = str(target_content) if target_content else "Processing..."

        # Handle wrapped content from interrupts
        if isinstance(target_content, dict):
            if target_content.get('type') == 'hitl_gate_interrupt':
                target_content = target_content.copy() # Avoid mutating original
                target_content['question'] = target_content.get('summary', target_content.get('message', 'Human review required'))
                # 'data' provides the context context
                target_content['context'] = target_content.get('data', {})
            elif target_content.get('type') == 'generic_interrupt':
                # Deep agents fallback strips the hitl payload into generic_interrupt data
                data = target_content.get('data', {})
                if isinstance(data, dict) and "question" in data:
                    target_content = target_content.copy()
                    target_content['question'] = data.get('question')
                    target_content['context'] = data.get('context', '')
                    target_content['phase'] = data.get('phase', 'unknown')
            elif target_content.get('type') == 'tool_call_approval_request':
                 target_content = target_content.copy()
                 target_content['question'] = target_content.get('reason', 'Tool execution requires approval')
                 target_content['context'] = f"Tool: {target_content.get('tool_name', 'unknown')}"
            elif target_content.get('type') == 'hitl_approval':
                 target_content = target_content.copy()
                 target_content['question'] = "Human Review Required"

                 # Extract action_requests — either directly or from original_interrupt
                 action_reqs = target_content.get('action_requests', [])
                 if not action_reqs and isinstance(target_content.get('original_interrupt'), dict):
                     action_reqs = target_content['original_interrupt'].get('action_requests', [])

                 # Build structured summary from action_requests
                 if action_reqs and isinstance(action_reqs, list):
                     context_text = self._format_action_requests_for_ui(action_reqs)
                     target_content['context'] = context_text

                     # Derive a meaningful phase from the dominant tool name
                     first_name = (
                         action_reqs[0].get('name', '')
                         if isinstance(action_reqs[0], dict)
                         else ''
                     )
                     target_content['phase'] = first_name or 'helm_operation'
                 else:
                     # Fallback: use the pre-formatted summary from the supervisor
                     target_content['context'] = target_content.get(
                         'summary', 'Action requires approval.',
                     )
                     target_content['phase'] = 'action_approval'
            elif 'pending_feedback_requests' in target_content:
                target_content = target_content['pending_feedback_requests']
            elif 'pending_approval' in target_content:
                target_content = target_content['pending_approval']
            elif 'pending_tool_calls' in target_content:
                tool_calls = target_content['pending_tool_calls']
                if tool_calls and isinstance(tool_calls, dict):
                    first_key = next(iter(tool_calls))
                    target_content = tool_calls[first_key].copy()
                    target_content['question'] = target_content.get('reason', 'Tool execution requires approval')
                    target_content['context'] = f"Tool: {target_content.get('tool_name', 'unknown')}"

        if isinstance(target_content, dict):
            # Prioritize 'question' if explicitly set (e.g. by hitl_approval), then fallback to 'summary'
            question = target_content.get('question', target_content.get('summary', target_content.get('message', content_str)))
            phase = target_content.get('phase', target_content.get('active_phase', ctx.phase))
            context_text = target_content.get('context', '')
            if isinstance(context_text, dict):
                context_text = json.dumps(context_text, indent=2)
            else:
                context_text = str(context_text)
        else:
            question = str(target_content)
            phase = ctx.phase
            context_text = ''
            
        phase_display = phase.replace('_', ' ').title() if phase else "Unknown"

        # ── Build Markdown Text ──────────────────────────────────────
        title_str = f"🔔 Input Required - {phase_display}"
        markdown_text = (
            f"### {title_str}\n\n"
            f"**{question}**\n\n"
            f"{context_text}\n"
        )

        column_children = [
            "markdown-text",
            "divider",
            "action-row",
        ]

        return [
            {
                "beginRendering": {
                    "surfaceId": "hitl-form",
                    "root": "approval-root",
                    "styles": {
                        "primaryColor": "#818cf8",
                        "foregroundColor": "#E2E8F0",
                        "font": "Inter",
                    }
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "hitl-form",
                    "components": [
                        {
                            "id": "approval-root",
                            "component": {
                                "Column": {
                                    "children": {
                                        "explicitList": column_children
                                    }
                                }
                            }
                        },
                        {
                            "id": "markdown-text",
                            "component": {
                                "Text": {
                                    "text": {"path": "markdown"},
                                    "usageHint": "body",
                                }
                            }
                        },
                        {"id": "divider", "component": {"Divider": {}}},
                        {
                            "id": "action-row",
                            "component": {
                                "Row": {
                                    "children": {"explicitList": ["reject-btn", "approve-btn"]},
                                    "distribution": "spaceEvenly"
                                }
                            }
                        },
                        {
                            "id": "reject-btn",
                            "component": {
                                "Button": {
                                    "child": "reject-text",
                                    "primary": False,
                                    "action": {
                                        "name": "hitl_response",
                                        "context": [
                                            {"key": "decision", "value": {"literalString": "reject"}},
                                            {"key": "phase", "value": {"path": "phaseId"}}
                                        ]
                                    }
                                }
                            }
                        },
                        {
                            "id": "reject-text",
                            "component": {
                                "Text": {"text": {"literalString": "❌ Reject"}}
                            }
                        },
                        {
                            "id": "approve-btn",
                            "component": {
                                "Button": {
                                    "child": "approve-text",
                                    "primary": True,
                                    "action": {
                                        "name": "hitl_response",
                                        "context": [
                                            {"key": "decision", "value": {"literalString": "approve"}},
                                            {"key": "phase", "value": {"path": "phaseId"}}
                                        ]
                                    }
                                }
                            }
                        },
                        {
                            "id": "approve-text",
                            "component": {
                                "Text": {"text": {"literalString": "✅ Approve"}}
                            }
                        }
                    ]
                }
            },
            {
                "dataModelUpdate": {
                    "surfaceId": "hitl-form",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text},
                        {"key": "phaseId", "valueString": phase if phase else "unknown"}
                    ]
                }
            }
        ]

