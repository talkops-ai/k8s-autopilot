"""App Operator Approval component — dedicated HITL card for App Operator domain.

Handles ArgoCD, Argo Rollouts, and Traefik approval cards with
domain-specific field resolution and rich descriptions.

Unlike the generic HitlApprovalComponent, this component:
  - Resolves ArgoCD entity names (name, app_name, repo_url, project_name)
  - Resolves ArgoCD namespaces (destination_namespace, dest_namespace)
  - Resolves Argo Rollouts entities (rollout_name, deployment_name)
  - Resolves Traefik entities (route_name, middleware_name, service_name)
  - Prefers the middleware-generated ``description`` over raw arg formatting
  - Renders a domain-appropriate card title and icon
"""

import json
from typing import Any, Dict, List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


# ---------------------------------------------------------------------------
# Tool name → (emoji, human label) for all App Operator domains
# ---------------------------------------------------------------------------

_APP_OPERATOR_TOOL_LABELS: dict[str, tuple[str, str]] = {
    # ArgoCD — Application Management
    "create_application": ("➕", "Create Application"),
    "update_application": ("✏️", "Update Application"),
    "delete_application": ("🗑️", "Delete Application"),
    "sync_application": ("🔄", "Sync Application"),
    "rollback_application": ("⏪", "Rollback Application"),
    "rollback_to_revision": ("⏪", "Rollback to Revision"),
    "hard_refresh": ("♻️", "Hard Refresh"),
    "soft_refresh": ("🔃", "Soft Refresh"),
    "cancel_deployment": ("🛑", "Cancel Deployment"),
    "prune_resources": ("🧹", "Prune Resources"),
    # ArgoCD — Repository Management
    "onboard_repository_https": ("📦", "Onboard Repository (HTTPS)"),
    "onboard_repository_ssh": ("🔑", "Onboard Repository (SSH)"),
    "delete_repository": ("🗑️", "Delete Repository"),
    # ArgoCD — Project Management
    "create_project": ("📁", "Create Project"),
    "delete_project": ("🗑️", "Delete Project"),
    # Argo Rollouts
    "argo_delete_rollout": ("🗑️", "Delete Rollout"),
    "argo_delete_experiment": ("🗑️", "Delete Experiment"),
    "convert_deployment_to_rollout": ("🔄", "Migration: Deployment → Rollout"),
    "convert_rollout_to_deployment": ("⏪", "Migration: Rollout → Deployment"),
    "argo_manage_rollout_lifecycle": ("🚀", "Rollout Lifecycle"),
    "argo_manage_legacy_deployment": ("⚠️", "Legacy Deployment Mutation"),
    # Traefik
    "traefik_manage_weighted_routing": ("🔀", "Weighted Routing"),
    "traefik_manage_simple_route": ("🔗", "Simple Route"),
    "traefik_manage_middleware": ("🛡️", "Middleware"),
    "traefik_nginx_migration": ("🔄", "NGINX Migration"),
    "traefik_manage_tcp_routing": ("🔌", "TCP Routing"),
    "traefik_configure_service_affinity": ("📌", "Sticky Sessions"),
}

# Tool names that belong to the App Operator domain (for dispatch detection)
_APP_OPERATOR_TOOLS: frozenset[str] = frozenset(_APP_OPERATOR_TOOL_LABELS.keys())


def _resolve_entity_name(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve the primary entity name from tool args, domain-aware.

    Resolution order varies per domain:
      ArgoCD apps:  name → app_name
      ArgoCD repos: repo_url
      ArgoCD proj:  project_name → name
      Rollouts:     name → rollout_name → deployment_name
      Traefik:      route_name → middleware_name → service_name → name
    """
    # ArgoCD repository tools
    if "repository" in tool_name:
        return args.get("repo_url", args.get("name", "unknown"))

    # ArgoCD project tools
    if "project" in tool_name:
        return args.get("project_name", args.get("name", "unknown"))

    # Traefik tools
    if "traefik" in tool_name:
        return (
            args.get("route_name")
            or args.get("middleware_name")
            or args.get("service_name")
            or args.get("name", "unknown")
        )

    # Argo Rollouts tools
    if "rollout" in tool_name or "deployment" in tool_name:
        return (
            args.get("name")
            or args.get("rollout_name")
            or args.get("deployment_name", "unknown")
        )

    # ArgoCD application tools (default)
    return args.get("name") or args.get("app_name", "unknown")


def _resolve_namespace(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve namespace from tool args, domain-aware.

    ArgoCD uses ``destination_namespace`` or ``dest_namespace``,
    everything else uses ``namespace``.
    """
    return (
        args.get("destination_namespace")
        or args.get("dest_namespace")
        or args.get("namespace")
        or "default"
    )


def _format_action_requests(action_requests: list) -> str:
    """Build a human-readable summary from HITL action_requests.

    Groups actions by tool name and formats each with entity name
    and namespace.  Uses the ``description`` field from the middleware
    if it is present (preferred — it is richer and domain-aware).

    Example output::

        🗑️ Delete Repository (1 action):
          • https://github.com/org/repo

        🔄 Sync Application (2 apps):
          • payment-service → namespace: production
          • frontend → namespace: default
    """
    # If there is a single action with a middleware-generated description,
    # use it directly — it's already formatted beautifully.
    if len(action_requests) == 1 and isinstance(action_requests[0], dict):
        desc = action_requests[0].get("description")
        if desc and isinstance(desc, str) and len(desc) > 20:
            return desc

    groups: Dict[str, list] = {}
    for req in action_requests:
        if not isinstance(req, dict):
            continue
        name = req.get("name", "unknown")
        args = req.get("args", {})
        desc = req.get("description", "")
        groups.setdefault(name, []).append({"args": args, "description": desc})

    lines: List[str] = []
    for tool_name, entries in groups.items():
        emoji, label = _APP_OPERATOR_TOOL_LABELS.get(
            tool_name,
            ("⚙️", tool_name.replace("_", " ").title()),
        )
        count = len(entries)

        # Determine dynamic resource label
        if "repository" in tool_name:
            item_label = "repo"
        elif "project" in tool_name:
            item_label = "project"
        elif "application" in tool_name or "sync" in tool_name:
            item_label = "app"
        elif "rollout" in tool_name or "experiment" in tool_name:
            item_label = "rollout"
        elif "traefik" in tool_name:
            item_label = "route"
        else:
            item_label = "action"

        plural = "s" if count != 1 else ""
        lines.append(f"{emoji} {label} ({count} {item_label}{plural}):")

        for entry in entries:
            args = entry["args"]
            # If middleware description is available and detailed, prefer it
            desc = entry.get("description", "")
            if desc and isinstance(desc, str) and len(desc) > 30:
                # Extract the first 2-3 key-value lines from the description
                for d_line in desc.split("\n"):
                    stripped = d_line.strip()
                    if stripped and stripped.startswith("**"):
                        lines.append(f"  {stripped}")
                continue

            entity = _resolve_entity_name(tool_name, args)
            ns = _resolve_namespace(tool_name, args)

            extras: List[str] = []
            if "version" in args:
                extras.append(f"v{args['version']}")
            if "revision" in args:
                extras.append(f"rev {args['revision']}")
            if "target_revision" in args:
                extras.append(f"rev {args['target_revision']}")
            if args.get("cascade") is False:
                extras.append("no cascade")
            if args.get("dry_run"):
                extras.append("dry run")
            suffix = f" ({', '.join(extras)})" if extras else ""

            # Repository tools: show URL, no namespace
            if "repository" in tool_name:
                lines.append(f"  • {entity}{suffix}")
            else:
                lines.append(f"  • {entity} → namespace: {ns}{suffix}")

        lines.append("")  # spacing between groups

    return "\n".join(lines).strip() or "Action requires approval."


@register_component(priority=25)
class AppOperatorApprovalComponent(BaseComponent):
    """Dedicated approval card for the App Operator deep agent.

    Handles ArgoCD, Argo Rollouts, and Traefik HITL approval
    interrupts with domain-specific entity resolution and
    middleware ``description`` passthrough.

    Higher priority (25) than the generic HitlApprovalComponent (20)
    so this component is checked first for App Operator interrupts.
    """

    component_type = "app_operator_approval"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False

        # Values confirmation is handled by another component
        if ctx.phase == "values_confirmation":
            return False

        # Detect App Operator domain by checking action_requests tool names
        if isinstance(ctx.content, dict):
            content = ctx.content
            ctype = content.get("type", "")

            # hitl_approval with action_requests from middleware
            if ctype == "hitl_approval":
                action_reqs = content.get("action_requests", [])
                if not action_reqs and isinstance(
                    content.get("original_interrupt"), dict,
                ):
                    action_reqs = content["original_interrupt"].get(
                        "action_requests", [],
                    )
                return self._has_app_operator_tools(action_reqs)

            # Direct tool_call_approval_request
            if ctype == "tool_call_approval_request":
                tool_name = content.get("tool_name", "")
                return tool_name in _APP_OPERATOR_TOOLS

        # Check metadata for phase hints
        interrupt_type = ctx.metadata.get("interrupt_type", "")
        if interrupt_type == "hitl_approval":
            action_reqs = ctx.metadata.get("action_requests", [])
            if not action_reqs and isinstance(ctx.content, dict):
                action_reqs = ctx.content.get("action_requests", [])
            return self._has_app_operator_tools(action_reqs)

        return False

    @staticmethod
    def _has_app_operator_tools(action_requests: list) -> bool:
        """Return True if any action_request targets an App Operator tool."""
        for req in action_requests:
            if not isinstance(req, dict):
                continue
            name = req.get("name", "")
            if name in _APP_OPERATOR_TOOLS:
                return True
        return False

    def build(self, ctx: RenderContext) -> List[dict]:
        target_content = ctx.content

        # ── Extract action_requests and build summary ──────────────
        action_reqs: list = []
        description_text = ""

        if isinstance(target_content, dict):
            ctype = target_content.get("type", "")

            if ctype == "hitl_approval":
                action_reqs = target_content.get("action_requests", [])
                if not action_reqs and isinstance(
                    target_content.get("original_interrupt"), dict,
                ):
                    action_reqs = target_content["original_interrupt"].get(
                        "action_requests", [],
                    )
                description_text = target_content.get("summary", "")

            elif ctype == "tool_call_approval_request":
                description_text = target_content.get(
                    "reason", "Tool execution requires approval",
                )

        # Build the summary from action_requests (or use description)
        if action_reqs:
            context_text = _format_action_requests(action_reqs)
            # Derive phase from dominant tool name
            first_name = ""
            if action_reqs and isinstance(action_reqs[0], dict):
                first_name = action_reqs[0].get("name", "")
            phase = first_name or "app_operation"
        else:
            context_text = description_text or "Action requires approval."
            phase = ctx.phase or "app_operation"

        question = "Human Review Required"
        phase_display = phase.replace("_", " ").title() if phase else "App Operation"

        # Determine title icon and label based on action type
        title_icon = "⚠️"
        if any(isinstance(r, dict) and "delete" in r.get("name", "")
               for r in action_reqs):
            title_icon = "🗑️"
        elif any(isinstance(r, dict) and "create" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "➕"
        elif any(isinstance(r, dict) and "sync" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "🔄"
        elif any(isinstance(r, dict) and "rollback" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "⏪"
        elif any(isinstance(r, dict) and "migration" in r.get("name", "")
                 or "convert" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "🔄"

        # ── Build Markdown Text ──────────────────────────────────────
        title_str = f"{title_icon} App Operation — {phase_display}"
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
                    "surfaceId": "app-op-approval",
                    "root": "approval-root",
                    "styles": {
                        "primaryColor": "#f97316",
                        "foregroundColor": "#E2E8F0",
                        "font": "Inter",
                    },
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "app-op-approval",
                    "components": [
                        {
                            "id": "approval-root",
                            "component": {
                                "Column": {
                                    "children": {
                                        "explicitList": column_children,
                                    }
                                }
                            },
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
                                    "children": {
                                        "explicitList": [
                                            "reject-btn",
                                            "approve-btn",
                                        ],
                                    },
                                    "distribution": "spaceEvenly",
                                }
                            },
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
                                            {
                                                "key": "decision",
                                                "value": {
                                                    "literalString": "reject",
                                                },
                                            },
                                            {
                                                "key": "phase",
                                                "value": {"path": "phaseId"},
                                            },
                                        ],
                                    },
                                }
                            },
                        },
                        {
                            "id": "reject-text",
                            "component": {
                                "Text": {
                                    "text": {"literalString": "❌ Reject"},
                                }
                            },
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
                                            {
                                                "key": "decision",
                                                "value": {
                                                    "literalString": "approve",
                                                },
                                            },
                                            {
                                                "key": "phase",
                                                "value": {"path": "phaseId"},
                                            },
                                        ],
                                    },
                                }
                            },
                        },
                        {
                            "id": "approve-text",
                            "component": {
                                "Text": {
                                    "text": {"literalString": "✅ Approve"},
                                }
                            },
                        },
                    ],
                }
            },
            {
                "dataModelUpdate": {
                    "surfaceId": "app-op-approval",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text},
                        {
                            "key": "phaseId",
                            "valueString": phase if phase else "unknown",
                        },
                    ],
                }
            },
        ]
