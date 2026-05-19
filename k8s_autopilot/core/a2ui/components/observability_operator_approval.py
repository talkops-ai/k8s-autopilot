"""Observability Operator Approval component — dedicated HITL card."""

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)

# ---------------------------------------------------------------------------
# Tool name → (emoji, human label) for Observability operations
# ---------------------------------------------------------------------------

_OBS_OPERATOR_TOOL_LABELS: dict[str, tuple[str, str]] = {
    # Prometheus Tools
    "prom_apply_servicemonitor": ("📡", "Apply ServiceMonitor"),
    "prom_apply_probe": ("🩺", "Apply Probe"),
    "prom_install_exporter": ("📦", "Install Exporter"),
    "prom_uninstall_exporter": ("🗑️", "Uninstall Exporter"),
    "prom_upsert_rule_group": ("📐", "Upsert Rule Group"),
    "prom_delete_rule_group": ("🗑️", "Delete Rule Group"),
    "prom_manage_file_sd": ("📝", "Manage File SD"),
    "prom_configure_remote_write": ("🔄", "Configure Remote Write"),
    # Alertmanager Tools
    "am_push_test_alert": ("🚨", "Push Test Alert"),
    "am_create_silence": ("🔇", "Create Silence"),
    "am_update_silence": ("⏱️", "Update Silence"),
    "am_expire_silence": ("🔊", "Expire Silence"),
    "am_silence_alert": ("🔇", "Silence Alert"),
}

_OBS_OPERATOR_TOOLS: frozenset[str] = frozenset(_OBS_OPERATOR_TOOL_LABELS.keys())


def _format_action_requests(action_requests: list) -> str:  # noqa: PLR0912, PLR0915
    """Build a human-readable summary from HITL action_requests."""
    if len(action_requests) == 1 and isinstance(action_requests[0], dict):
        desc = action_requests[0].get("description")
        if desc and isinstance(desc, str) and len(desc) > 20:
            return desc

    groups: dict[str, list] = {}
    for req in action_requests:
        if not isinstance(req, dict):
            continue
        name = req.get("name", "unknown")
        args = req.get("args", {})
        desc = req.get("description", "")
        groups.setdefault(name, []).append({"args": args, "description": desc})

    lines: list[str] = []
    for tool_name, entries in groups.items():
        emoji, label = _OBS_OPERATOR_TOOL_LABELS.get(
            tool_name,
            ("⚙️", tool_name.replace("_", " ").title()),
        )
        count = len(entries)
        plural = "s" if count != 1 else ""
        lines.append(f"{emoji} **{label}** ({count} action{plural}):")

        for entry in entries:
            args = entry["args"]
            desc = entry.get("description", "")
            if desc and isinstance(desc, str) and len(desc) > 30:
                for d_line in desc.split("\n"):
                    stripped = d_line.strip()
                    if stripped and stripped.startswith(("**", "⚠", "🚨")):
                        lines.append(f"  {stripped}")
                continue

            # Format specifically for different observability tools
            if tool_name == "prom_apply_servicemonitor":
                svc = args.get("service_name", "unknown")
                ns = args.get("namespace", "default")
                lines.append(f"  • **ServiceMonitor**: `{svc}` → namespace: `{ns}`")

                # Render other parameters nicely
                for k, v in args.items():
                    if k not in ("service_name", "namespace"):
                        lines.append(f"    - {k}: `{v}`")

            elif tool_name == "prom_apply_probe":
                probe = args.get("probe_name", "unknown")
                ns = args.get("namespace", "default")
                targets = args.get("targets", [])
                lines.append(f"  • **Probe**: `{probe}` → namespace: `{ns}`")
                if targets:
                    lines.append(f"    - Targets: {', '.join(targets)}")
                for k, v in args.items():
                    if k not in ("probe_name", "namespace", "targets"):
                        lines.append(f"    - {k}: `{v}`")

            elif tool_name in ("prom_install_exporter", "prom_uninstall_exporter"):
                exp = args.get("exporter_type", "unknown")
                ns = args.get("namespace", "default")
                lines.append(f"  • **Exporter**: `{exp}` → namespace: `{ns}`")
                for k, v in args.items():
                    if k not in ("exporter_type", "namespace"):
                        lines.append(f"    - {k}: `{v}`")

            elif tool_name in ("prom_upsert_rule_group", "prom_delete_rule_group"):
                grp = args.get("group_name", "unknown")
                ns = args.get("namespace", "default")
                lines.append(f"  • **Rule Group**: `{grp}` → namespace: `{ns}`")
                if tool_name == "prom_upsert_rule_group" and "rules" in args:
                    lines.append(f"    - Rules count: {len(args['rules'])}")

            elif tool_name == "am_create_silence":
                duration = args.get("duration_minutes", "unknown")
                comment = args.get("comment", "No comment provided")
                lines.append(f"  • **Silence** for {duration}m: _{comment}_")
                matchers = args.get("matchers", [])
                lines.extend(f"    - Matcher: {m}" for m in matchers)

            elif tool_name == "am_push_test_alert":
                labels = args.get("alert_labels", {})
                alertname = labels.get("alertname", "unknown")
                lines.append(f"  • **Test Alert**: `{alertname}`")
                for k, v in labels.items():
                    if k != "alertname":
                        lines.append(f"    - Label `{k}`: `{v}`")
                annotations = args.get("annotations", {})
                for k, v in annotations.items():
                    lines.append(f"    - Annotation `{k}`: `{v}`")

            else:
                # Generic dump for other tools
                lines.append("  • **Parameters**:")
                for k, v in args.items():
                    lines.append(f"    - {k}: `{v}`")

        lines.append("")

    return "\n".join(lines).strip() or "Observability operation requires approval."


@register_component(priority=27)
class ObservabilityOperatorApprovalComponent(BaseComponent):
    """Dedicated approval card for the Observability operator.

    Handles Prometheus and Alertmanager operations HITL approval interrupts with
    domain-specific parameter extraction and rich Markdown previews.

    Higher priority (27) than K8sOperatorApprovalComponent (26) and
    generic HitlApprovalComponent (20).
    """

    component_type = "observability_operator_approval"

    def can_handle(self, ctx: RenderContext) -> bool:  # noqa: PLR0911
        if not ctx.require_user_input:
            return False

        if ctx.phase == "values_confirmation":
            return False

        if isinstance(ctx.content, dict):
            content = ctx.content
            ctype = content.get("type", "")

            # hitl_approval with action_requests from middleware
            if ctype == "hitl_approval":
                action_reqs = content.get("action_requests", [])
                if not action_reqs and isinstance(
                    content.get("original_interrupt"),
                    dict,
                ):
                    action_reqs = content["original_interrupt"].get(
                        "action_requests",
                        [],
                    )
                return self._has_obs_operator_tools(action_reqs)

            # Direct tool_call_approval_request
            if ctype == "tool_call_approval_request":
                tool_name = content.get("tool_name", "")
                return tool_name in _OBS_OPERATOR_TOOLS

            # pending_tool_calls block
            if "pending_tool_calls" in content:
                tool_calls = content["pending_tool_calls"]
                if tool_calls and isinstance(tool_calls, dict):
                    first_key = next(iter(tool_calls))
                    tool_name = tool_calls[first_key].get("tool_name", "")
                    return tool_name in _OBS_OPERATOR_TOOLS

        # Check metadata for phase hints
        interrupt_type = ctx.metadata.get("interrupt_type", "")
        if interrupt_type == "hitl_approval":
            action_reqs = ctx.metadata.get("action_requests", [])
            if not action_reqs and isinstance(ctx.content, dict):
                action_reqs = ctx.content.get("action_requests", [])
            return self._has_obs_operator_tools(action_reqs)

        return False

    @staticmethod
    def _has_obs_operator_tools(action_requests: list) -> bool:
        for req in action_requests:
            if not isinstance(req, dict):
                continue
            name = req.get("name", "")
            if name in _OBS_OPERATOR_TOOLS:
                return True
        return False

    def build(self, ctx: RenderContext) -> list[dict]:  # noqa: PLR0912
        target_content = ctx.content
        action_reqs: list = []
        description_text = ""

        if isinstance(target_content, dict):
            ctype = target_content.get("type", "")

            if ctype == "hitl_approval":
                action_reqs = target_content.get("action_requests", [])
                if not action_reqs and isinstance(
                    target_content.get("original_interrupt"),
                    dict,
                ):
                    action_reqs = target_content["original_interrupt"].get(
                        "action_requests",
                        [],
                    )
                description_text = target_content.get("summary", "")

            elif ctype == "tool_call_approval_request":
                # Ensure we handle a single tool_args correctly
                tool_args = target_content.get("tool_args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}
                action_reqs = [
                    {"name": target_content.get("tool_name", ""), "args": tool_args},
                ]
                description_text = target_content.get(
                    "reason",
                    "Tool execution requires approval",
                )
            elif "pending_tool_calls" in target_content:
                # Same as hitl_approval.py logic for raw state
                tool_calls = target_content["pending_tool_calls"]
                if tool_calls and isinstance(tool_calls, dict):
                    first_key = next(iter(tool_calls))
                    tool_call = tool_calls[first_key]
                    action_reqs = [
                        {
                            "name": tool_call.get("tool_name", ""),
                            "args": tool_call.get("tool_args", {}),
                        },
                    ]

        if action_reqs:
            context_text = _format_action_requests(action_reqs)
            first_name = ""
            if action_reqs and isinstance(action_reqs[0], dict):
                first_name = action_reqs[0].get("name", "")
            phase = first_name or "observability_operation"
        else:
            context_text = (
                description_text or "Observability operation requires approval."
            )
            phase = ctx.phase or "observability_operation"

        question = "Observability Operation — Human Review Required"
        phase_display = (
            phase.replace("_", " ").title() if phase else "Observability Operation"
        )

        # Determine title icon based on action type
        title_icon = "👁️"
        if any(
            isinstance(r, dict) and "alert" in r.get("name", "") for r in action_reqs
        ):
            title_icon = "🚨"
        elif any(
            isinstance(r, dict) and "silence" in r.get("name", "") for r in action_reqs
        ):
            title_icon = "🔇"
        elif any(
            isinstance(r, dict) and "exporter" in r.get("name", "") for r in action_reqs
        ):
            title_icon = "📦"
        elif any(
            isinstance(r, dict) and "rule" in r.get("name", "") for r in action_reqs
        ):
            title_icon = "📐"

        title_str = f"{title_icon} Observability Operation — {phase_display}"
        markdown_text = f"### {title_str}\n\n**{question}**\n\n{context_text}\n"

        column_children = [
            "markdown-text",
            "divider",
            "obs-action-row",
        ]

        return [
            {
                "beginRendering": {
                    "surfaceId": "obs-op-approval",
                    "root": "obs-approval-root",
                    "styles": {
                        "primaryColor": "#f59e0b",  # Amber/Orange for Observability
                        "foregroundColor": "#E2E8F0",
                        "font": "Inter",
                    },
                },
            },
            {
                "surfaceUpdate": {
                    "surfaceId": "obs-op-approval",
                    "components": [
                        {
                            "id": "obs-approval-root",
                            "component": {
                                "Column": {
                                    "children": {
                                        "explicitList": column_children,
                                    },
                                },
                            },
                        },
                        {
                            "id": "markdown-text",
                            "component": {
                                "Text": {
                                    "text": {"path": "markdown"},
                                    "usageHint": "body",
                                },
                            },
                        },
                        {"id": "divider", "component": {"Divider": {}}},
                        {
                            "id": "obs-action-row",
                            "component": {
                                "Row": {
                                    "children": {
                                        "explicitList": [
                                            "obs-reject-btn",
                                            "obs-approve-btn",
                                        ],
                                    },
                                    "distribution": "spaceEvenly",
                                },
                            },
                        },
                        {
                            "id": "obs-reject-btn",
                            "component": {
                                "Button": {
                                    "child": "obs-reject-text",
                                    "primary": False,
                                    "action": {
                                        "name": "hitl_response",
                                        "context": [
                                            {
                                                "key": "decision",
                                                "value": {"literalString": "reject"},
                                            },
                                            {
                                                "key": "phase",
                                                "value": {"path": "phaseId"},
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                        {
                            "id": "obs-reject-text",
                            "component": {
                                "Text": {
                                    "text": {"literalString": "❌ Reject"},
                                },
                            },
                        },
                        {
                            "id": "obs-approve-btn",
                            "component": {
                                "Button": {
                                    "child": "obs-approve-text",
                                    "primary": True,
                                    "action": {
                                        "name": "hitl_response",
                                        "context": [
                                            {
                                                "key": "decision",
                                                "value": {"literalString": "approve"},
                                            },
                                            {
                                                "key": "phase",
                                                "value": {"path": "phaseId"},
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                        {
                            "id": "obs-approve-text",
                            "component": {
                                "Text": {
                                    "text": {"literalString": "✅ Approve"},
                                },
                            },
                        },
                    ],
                },
            },
            {
                "dataModelUpdate": {
                    "surfaceId": "obs-op-approval",
                    "path": "/",
                    "contents": [
                        {"key": "markdown", "valueString": markdown_text},
                        {
                            "key": "phaseId",
                            "valueString": phase if phase else "unknown",
                        },
                    ],
                },
            },
        ]
