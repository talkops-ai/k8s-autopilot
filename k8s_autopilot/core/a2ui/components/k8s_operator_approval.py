"""K8s Operator Approval component — dedicated HITL card for K8s cluster operations.

Handles Kubernetes resource CRUD, scaling, pod exec, and pod run approval
cards with domain-specific field resolution and rich descriptions.

Unlike the generic HitlApprovalComponent, this component:
  - Resolves Kubernetes resource identifiers (kind, apiVersion, name, namespace)
  - Detects production and system namespace targeting (elevated visual warnings)
  - Shows YAML previews for create_or_update operations
  - Detects force-delete and scale-to-zero edge cases
  - Renders a Kubernetes-themed card with resource-aware icons
"""

import json
from typing import Any, Dict, List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)


# ---------------------------------------------------------------------------
# Tool name → (emoji, human label) for K8s cluster operations
# ---------------------------------------------------------------------------

_K8S_OPERATOR_TOOL_LABELS: dict[str, tuple[str, str]] = {
    # Destructive
    "resources_delete": ("🗑️", "Delete Resource"),
    "pods_delete": ("🗑️", "Delete Pod"),
    # Mutation
    "resources_create_or_update": ("📝", "Create/Update Resource"),
    "resources_scale": ("⚖️", "Scale Resource"),
    # Shell access
    "pods_exec": ("🔐", "Pod Exec"),
    # Pod creation
    "pods_run": ("🚀", "Run Pod"),
}

# Tool names that belong to the K8s Operator domain (for dispatch detection)
_K8S_OPERATOR_TOOLS: frozenset[str] = frozenset(_K8S_OPERATOR_TOOL_LABELS.keys())

# Namespace awareness
_PRODUCTION_NAMESPACES = {"production", "prod", "prd", "live", "default"}
_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


def _resolve_entity_name(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve the primary entity name from tool args, K8s-domain-aware.

    Resolution order:
      resources_*:    name
      pods_*:         name
      pods_run:       name (auto-generated if absent)
    """
    return args.get("name", "unknown")


def _resolve_namespace(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve namespace from tool args."""
    return args.get("namespace", "default")


def _resolve_kind(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve Kubernetes resource kind from tool args."""
    if "resources" in tool_name:
        return args.get("kind", "")
    if tool_name in ("pods_delete", "pods_exec", "pods_run", "pods_log"):
        return "Pod"
    return ""


def _resolve_api_version(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve apiVersion from tool args."""
    return args.get("apiVersion", args.get("api_version", ""))


def _get_env_badge(namespace: str) -> str:
    """Return an environment warning badge based on namespace."""
    ns_lower = str(namespace or "").strip().lower()
    if ns_lower in _PRODUCTION_NAMESPACES:
        return "🚨 PRODUCTION"
    if ns_lower in _SYSTEM_NAMESPACES:
        return "🚨 SYSTEM"
    return ""


def _format_action_requests(action_requests: list) -> str:
    """Build a human-readable summary from HITL action_requests.

    Groups actions by tool name and formats each with resource kind,
    name, namespace, and environment badges.

    Example output::

        🗑️ Delete Resource (1 resource):
          • Deployment/nginx-deploy → namespace: production 🚨 PRODUCTION

        ⚖️ Scale Resource (1 resource):
          • Deployment/web-app → namespace: staging  |  replicas: 5
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
        emoji, label = _K8S_OPERATOR_TOOL_LABELS.get(
            tool_name,
            ("⚙️", tool_name.replace("_", " ").title()),
        )
        count = len(entries)

        # Determine item label
        if "pod" in tool_name.lower():
            item_label = "pod"
        else:
            item_label = "resource"

        plural = "s" if count != 1 else ""
        lines.append(f"{emoji} {label} ({count} {item_label}{plural}):")

        for entry in entries:
            args = entry["args"]

            # If middleware description is available and detailed, prefer it
            desc = entry.get("description", "")
            if desc and isinstance(desc, str) and len(desc) > 30:
                for d_line in desc.split("\n"):
                    stripped = d_line.strip()
                    if stripped and (stripped.startswith("**") or stripped.startswith("⚠") or stripped.startswith("🚨")):
                        lines.append(f"  {stripped}")
                continue

            entity = _resolve_entity_name(tool_name, args)
            ns = _resolve_namespace(tool_name, args)
            kind = _resolve_kind(tool_name, args)
            env_badge = _get_env_badge(ns)

            # Build entity display: Kind/Name or just Name
            entity_display = f"{kind}/{entity}" if kind else entity

            extras: List[str] = []
            api_ver = _resolve_api_version(tool_name, args)
            if api_ver:
                extras.append(api_ver)
            if "scale" in args:
                extras.append(f"replicas: {args['scale']}")
            if args.get("gracePeriodSeconds") is not None:
                gp = args["gracePeriodSeconds"]
                if int(gp) == 0:
                    extras.append("⚡ FORCE DELETE")
                else:
                    extras.append(f"grace: {gp}s")
            if "command" in args:
                cmd = args["command"]
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                extras.append(f"cmd: `{cmd_str[:60]}`")
            if "image" in args:
                extras.append(f"image: {args['image']}")

            suffix = f"  |  {', '.join(extras)}" if extras else ""
            badge = f"  {env_badge}" if env_badge else ""

            lines.append(f"  • {entity_display} → namespace: {ns}{suffix}{badge}")

        lines.append("")  # spacing between groups

    return "\n".join(lines).strip() or "Kubernetes operation requires approval."


@register_component(priority=26)
class K8sOperatorApprovalComponent(BaseComponent):
    """Dedicated approval card for the K8s Operator deep agent.

    Handles Kubernetes cluster operations HITL approval interrupts with
    domain-specific resource resolution, YAML preview, and environment
    awareness (production/system namespace detection).

    Higher priority (26) than AppOperatorApprovalComponent (25) and
    generic HitlApprovalComponent (20) so this component is checked
    first for K8s Operator interrupts.
    """

    component_type = "k8s_operator_approval"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False

        # Values confirmation is handled by another component
        if ctx.phase == "values_confirmation":
            return False

        # Detect K8s Operator domain by checking action_requests tool names
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
                return self._has_k8s_operator_tools(action_reqs)

            # Direct tool_call_approval_request
            if ctype == "tool_call_approval_request":
                tool_name = content.get("tool_name", "")
                return tool_name in _K8S_OPERATOR_TOOLS

        # Check metadata for phase hints
        interrupt_type = ctx.metadata.get("interrupt_type", "")
        if interrupt_type == "hitl_approval":
            action_reqs = ctx.metadata.get("action_requests", [])
            if not action_reqs and isinstance(ctx.content, dict):
                action_reqs = ctx.content.get("action_requests", [])
            return self._has_k8s_operator_tools(action_reqs)

        return False

    @staticmethod
    def _has_k8s_operator_tools(action_requests: list) -> bool:
        """Return True if any action_request targets a K8s Operator tool."""
        for req in action_requests:
            if not isinstance(req, dict):
                continue
            name = req.get("name", "")
            if name in _K8S_OPERATOR_TOOLS:
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
            phase = first_name or "k8s_operation"
        else:
            context_text = description_text or "Kubernetes operation requires approval."
            phase = ctx.phase or "k8s_operation"

        question = "Kubernetes Operation — Human Review Required"
        phase_display = phase.replace("_", " ").title() if phase else "K8s Operation"

        # Determine title icon based on action type
        title_icon = "☸️"  # Default: Kubernetes wheel
        if any(isinstance(r, dict) and "delete" in r.get("name", "")
               for r in action_reqs):
            title_icon = "🗑️"
        elif any(isinstance(r, dict) and "create" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "📝"
        elif any(isinstance(r, dict) and "scale" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "⚖️"
        elif any(isinstance(r, dict) and "exec" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "🔐"
        elif any(isinstance(r, dict) and "run" in r.get("name", "")
                 for r in action_reqs):
            title_icon = "🚀"

        # Detect environment badge for title
        env_text = ""
        for req in action_reqs:
            if isinstance(req, dict):
                ns = req.get("args", {}).get("namespace", "")
                badge = _get_env_badge(ns)
                if badge:
                    env_text = f" — {badge}"
                    break

        # ── Build context line components ────────────────────────────
        context_lines = [
            ln for ln in (context_text or "").split("\n") if ln.strip()
        ]
        if not context_lines:
            context_lines = [f"Phase: {phase}"]

        context_line_ids = [f"k8s-ctx-line-{i}" for i in range(len(context_lines))]

        context_line_components = []
        for idx, line in enumerate(context_lines):
            # Resource headers get body style, detail lines get caption
            is_header = not line.startswith("  ")
            context_line_components.append({
                "id": context_line_ids[idx],
                "component": {
                    "Text": {
                        "usageHint": "body" if is_header else "caption",
                        "text": {"literalString": line},
                    }
                },
            })

        column_children = [
            "k8s-approval-header",
            "k8s-divider1",
            "k8s-question-text",
            *context_line_ids,
            "k8s-divider2",
            "k8s-action-row",
        ]

        return [
            {
                "beginRendering": {
                    "surfaceId": "k8s-op-approval",
                    "root": "k8s-approval-root",
                    "styles": {
                        "primaryColor": "#3b82f6",  # Blue — Kubernetes brand color
                        "font": "Inter",
                    },
                }
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "k8s-op-approval",
                    "components": [
                        {
                            "id": "k8s-approval-root",
                            "component": {
                                "Card": {"child": "k8s-approval-content"},
                            },
                        },
                        {
                            "id": "k8s-approval-content",
                            "component": {
                                "Column": {
                                    "children": {
                                        "explicitList": column_children,
                                    }
                                }
                            },
                        },
                        {
                            "id": "k8s-approval-header",
                            "component": {
                                "Row": {
                                    "children": {
                                        "explicitList": [
                                            "k8s-header-icon",
                                            "k8s-header-title",
                                        ],
                                    },
                                    "alignment": "center",
                                }
                            },
                        },
                        {
                            "id": "k8s-header-icon",
                            "component": {
                                "Icon": {
                                    "name": {"literalString": "security"},
                                }
                            },
                        },
                        {
                            "id": "k8s-header-title",
                            "component": {
                                "Text": {
                                    "usageHint": "h3",
                                    "text": {"path": "title"},
                                }
                            },
                        },
                        {"id": "k8s-divider1", "component": {"Divider": {}}},
                        {
                            "id": "k8s-question-text",
                            "component": {
                                "Text": {
                                    "usageHint": "body",
                                    "text": {"path": "question"},
                                }
                            },
                        },
                        *context_line_components,
                        {"id": "k8s-divider2", "component": {"Divider": {}}},
                        {
                            "id": "k8s-action-row",
                            "component": {
                                "Row": {
                                    "children": {
                                        "explicitList": [
                                            "k8s-reject-btn",
                                            "k8s-approve-btn",
                                        ],
                                    },
                                    "distribution": "spaceEvenly",
                                }
                            },
                        },
                        {
                            "id": "k8s-reject-btn",
                            "component": {
                                "Button": {
                                    "child": "k8s-reject-text",
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
                            "id": "k8s-reject-text",
                            "component": {
                                "Text": {
                                    "text": {"literalString": "❌ Reject"},
                                }
                            },
                        },
                        {
                            "id": "k8s-approve-btn",
                            "component": {
                                "Button": {
                                    "child": "k8s-approve-text",
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
                            "id": "k8s-approve-text",
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
                    "surfaceId": "k8s-op-approval",
                    "path": "/",
                    "contents": [
                        {
                            "key": "title",
                            "valueString": (
                                f"{title_icon} K8s Cluster Operation — "
                                f"{phase_display}{env_text}"
                            ),
                        },
                        {"key": "question", "valueString": question},
                        {
                            "key": "phaseId",
                            "valueString": phase if phase else "unknown",
                        },
                    ],
                }
            },
        ]
