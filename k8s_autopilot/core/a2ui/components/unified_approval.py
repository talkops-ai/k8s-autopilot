"""Unified Approval component — single HITL card for ALL tool-call approvals.

Replaces the domain-specific approval components (App, K8s, Observability)
and the generic HitlApprovalComponent with ONE data-driven component.

Uses ``TOOL_LABEL_REGISTRY`` to resolve tool names → (emoji, label, domain,
item_label) dynamically.  All domain-specific formatting (entity resolution,
namespace extraction, environment badges) is handled by a single set of
resolver functions.

Renders the ``hitlApprovalCard`` with **2 buttons**: Approve / Reject.
(Plan review uses 3 buttons and is handled by ``PlanningApprovalComponent``.)
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from k8s_autopilot.core.a2ui.registry import (
    BaseComponent,
    RenderContext,
    register_component,
)
from k8s_autopilot.core.a2ui.param_extractor import extract_parameters
from k8s_autopilot.core.a2ui.risk_classification import classify_risk
from k8s_autopilot.core.a2ui.surface_builder import build_hitl_approval_surface


# ---------------------------------------------------------------------------
# TOOL_LABEL_REGISTRY — tool_name → (emoji, label, domain, item_label)
# ---------------------------------------------------------------------------

TOOL_LABEL_REGISTRY: dict[str, tuple[str, str, str, str]] = {
    # ── Helm ──────────────────────────────────────────────────────────
    "helm_install_chart":       ("🚀", "Install",          "helm",     "release"),
    "helm_upgrade_release":     ("⬆️", "Upgrade",          "helm",     "release"),
    "helm_rollback_release":    ("⏪", "Rollback",         "helm",     "release"),
    "helm_uninstall_release":   ("🗑️", "Uninstall",        "helm",     "release"),
    # ── ArgoCD — Application ──────────────────────────────────────────
    "create_application":       ("➕", "Create Application",    "argocd",   "app"),
    "update_application":       ("✏️", "Update Application",    "argocd",   "app"),
    "delete_application":       ("🗑️", "Delete Application",    "argocd",   "app"),
    "sync_application":         ("🔄", "Sync Application",      "argocd",   "app"),
    "rollback_application":     ("⏪", "Rollback Application",  "argocd",   "app"),
    "rollback_to_revision":     ("⏪", "Rollback to Revision",  "argocd",   "app"),
    "hard_refresh":             ("♻️", "Hard Refresh",          "argocd",   "app"),
    "soft_refresh":             ("🔃", "Soft Refresh",          "argocd",   "app"),
    "cancel_deployment":        ("🛑", "Cancel Deployment",     "argocd",   "app"),
    "prune_resources":          ("🧹", "Prune Resources",       "argocd",   "app"),
    # ── ArgoCD — Repository ───────────────────────────────────────────
    "onboard_repository_https": ("📦", "Onboard Repo (HTTPS)",  "argocd",   "repo"),
    "onboard_repository_ssh":   ("🔑", "Onboard Repo (SSH)",    "argocd",   "repo"),
    "delete_repository":        ("🗑️", "Delete Repository",     "argocd",   "repo"),
    # ── ArgoCD — Project ──────────────────────────────────────────────
    "create_project":           ("📁", "Create Project",         "argocd",   "project"),
    "delete_project":           ("🗑️", "Delete Project",         "argocd",   "project"),
    # ── Argo Rollouts ─────────────────────────────────────────────────
    "argo_rollouts_promote":            ("🟢", "Promote Rollout",   "rollouts", "rollout"),
    "argo_rollouts_abort":              ("🛑", "Abort Rollout",     "rollouts", "rollout"),
    "argo_rollouts_retry":              ("🔁", "Retry Rollout",     "rollouts", "rollout"),
    "argo_delete_rollout":              ("🗑️", "Delete Rollout",    "rollouts", "rollout"),
    "argo_delete_experiment":           ("🗑️", "Delete Experiment", "rollouts", "rollout"),
    "convert_deployment_to_rollout":    ("🔄", "Deployment → Rollout",  "rollouts", "rollout"),
    "convert_rollout_to_deployment":    ("⏪", "Rollout → Deployment",  "rollouts", "rollout"),
    "argo_manage_rollout_lifecycle":    ("🚀", "Rollout Lifecycle",     "rollouts", "rollout"),
    "argo_manage_legacy_deployment":   ("⚠️", "Legacy Deployment",     "rollouts", "rollout"),
    # ── Traefik ───────────────────────────────────────────────────────
    "traefik_manage_weighted_routing":      ("🔀", "Weighted Routing",   "traefik", "route"),
    "traefik_manage_simple_route":          ("🔗", "Simple Route",       "traefik", "route"),
    "traefik_manage_middleware":            ("🛡️", "Middleware",          "traefik", "middleware"),
    "traefik_nginx_migration":             ("🔄", "NGINX Migration",     "traefik", "route"),
    "traefik_manage_tcp_routing":           ("🔌", "TCP Routing",        "traefik", "route"),
    "traefik_configure_service_affinity":   ("📌", "Sticky Sessions",    "traefik", "config"),
    "traefik_apply_ingressroute":           ("🔀", "Apply IngressRoute", "traefik", "route"),
    "traefik_delete_ingressroute":          ("🗑️", "Delete IngressRoute","traefik", "route"),
    "traefik_apply_middleware":             ("🔧", "Apply Middleware",   "traefik", "middleware"),
    "traefik_delete_middleware":            ("🗑️", "Delete Middleware",  "traefik", "middleware"),
    "traefik_apply_tlsoption":             ("🔒", "Apply TLS Option",   "traefik", "config"),
    # ── K8s Cluster ───────────────────────────────────────────────────
    "resources_delete":             ("🗑️", "Delete Resource",      "k8s", "resource"),
    "pods_delete":                  ("🗑️", "Delete Pod",           "k8s", "pod"),
    "resources_create_or_update":   ("📝", "Create/Update",        "k8s", "resource"),
    "resources_scale":              ("⚖️", "Scale Resource",       "k8s", "resource"),
    "pods_exec":                    ("🔐", "Pod Exec",             "k8s", "pod"),
    "pods_run":                     ("🚀", "Run Pod",              "k8s", "pod"),
    # ── Observability — Prometheus ────────────────────────────────────
    "prom_apply_servicemonitor":    ("📡", "Apply ServiceMonitor", "obs", "action"),
    "prom_apply_probe":             ("🩺", "Apply Probe",          "obs", "action"),
    "prom_install_exporter":        ("📦", "Install Exporter",     "obs", "action"),
    "prom_uninstall_exporter":      ("🗑️", "Uninstall Exporter",  "obs", "action"),
    "prom_upsert_rule_group":       ("📐", "Upsert Rule Group",   "obs", "action"),
    "prom_delete_rule_group":       ("🗑️", "Delete Rule Group",   "obs", "action"),
    "prom_manage_file_sd":          ("📝", "Manage File SD",       "obs", "action"),
    "prom_configure_remote_write":  ("🔄", "Configure Remote Write", "obs", "action"),
    # ── Observability — Alertmanager ──────────────────────────────────
    "am_push_test_alert":           ("🚨", "Push Test Alert",      "obs", "action"),
    "am_create_silence":            ("🔇", "Create Silence",       "obs", "action"),
    "am_update_silence":            ("⏱️", "Update Silence",       "obs", "action"),
    "am_expire_silence":            ("🔊", "Expire Silence",       "obs", "action"),
    "am_silence_alert":             ("🔇", "Silence Alert",        "obs", "action"),
    # ── Observability — OpenTelemetry ────────────────────────────────
    "otel_provision_collector":         ("📦", "Provision Collector",     "obs", "action"),
    "otel_patch_collector":             ("🔧", "Patch Collector",         "obs", "action"),
    "otel_patch_instrumentation":       ("🔌", "Patch Instrumentation",   "obs", "action"),
    "otel_annotate_deployment":         ("🚀", "Annotate Deployment",     "obs", "action"),
    "otel_toggle_sampling_strategy":    ("📊", "Toggle Sampling",         "obs", "action"),
    "otel_enable_spanmetrics_for_service": ("📈", "Enable SpanMetrics",   "obs", "action"),
    # ── Observability — Tempo ────────────────────────────────────────
    "tempo_create_operator_cr":          ("➕", "Create Tempo CR",       "obs", "action"),
    "tempo_patch_operator_cr":           ("🔧", "Patch Tempo CR",        "obs", "action"),
}

# All known tool names for fast lookup
_ALL_KNOWN_TOOLS: frozenset[str] = frozenset(TOOL_LABEL_REGISTRY.keys())

# Namespace awareness
_PRODUCTION_NAMESPACES = {"production", "prod", "prd", "live", "default"}
_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


# ---------------------------------------------------------------------------
# Entity / namespace resolvers — unified across all domains
# ---------------------------------------------------------------------------

def _resolve_entity_name(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve the primary entity name from tool args, domain-aware."""
    # ArgoCD repository tools
    if "repository" in tool_name:
        return str(args.get("repo_url", args.get("name", "unknown")))

    # ArgoCD project tools
    if "project" in tool_name:
        return str(args.get("project_name", args.get("name", "unknown")))

    # Traefik tools
    if "traefik" in tool_name:
        return str(
            args.get("route_name")
            or args.get("middleware_name")
            or args.get("service_name")
            or args.get("name", "unknown")
        )

    # Argo Rollouts tools
    if "rollout" in tool_name or "deployment" in tool_name:
        return str(
            args.get("name")
            or args.get("rollout_name")
            or args.get("deployment_name", "unknown")
        )

    # K8s resource tools — Kind/Name format
    if "resources" in tool_name or "pods" in tool_name:
        name = str(args.get("name", "unknown"))
        kind = str(args.get("kind", ""))
        if kind:
            return f"{kind}/{name}"
        return name

    # Helm tools
    if "helm" in tool_name:
        return str(
            args.get("release_name")
            or args.get("chart_name")
            or args.get("name", "unknown")
        )

    # ArgoCD application tools (default)
    return str(args.get("name") or args.get("app_name", "unknown"))


def _resolve_namespace(tool_name: str, args: Dict[str, Any]) -> str:
    """Resolve namespace from tool args, domain-aware."""
    return str(
        args.get("destination_namespace")
        or args.get("dest_namespace")
        or args.get("namespace")
        or "default"
    )


def _get_env_badge(namespace: str) -> str:
    """Return an environment warning badge based on namespace."""
    ns_lower = str(namespace or "").strip().lower()
    if ns_lower in _PRODUCTION_NAMESPACES:
        return "🚨 PRODUCTION"
    if ns_lower in _SYSTEM_NAMESPACES:
        return "🚨 SYSTEM"
    return ""


# ---------------------------------------------------------------------------
# Unified action_requests formatter
# ---------------------------------------------------------------------------

def _format_action_requests(action_requests: list) -> str:
    """Build a human-readable summary from HITL action_requests.

    Groups actions by tool name and formats each with entity name
    and namespace.  Uses the ``description`` field from the middleware
    if it is present (preferred — it is richer and domain-aware).

    Example output::

        ⏪ Rollback (1 release):
          • cart → namespace: otel-demo  (rev 6)

        🗑️ Delete Resource (2 resources):
          • Deployment/nginx → namespace: production 🚨 PRODUCTION
          • Service/old-svc → namespace: default
    """
    # Single action with a middleware-generated description → use it directly
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
        # Look up from registry or fall back to humanized name
        registry_entry = TOOL_LABEL_REGISTRY.get(tool_name)
        if registry_entry:
            emoji, label, _domain, item_label = registry_entry
        else:
            emoji = "⚙️"
            label = tool_name.replace("_", " ").title()
            item_label = "action"

        count = len(entries)
        plural = "s" if count != 1 else ""
        lines.append(f"{emoji} {label} ({count} {item_label}{plural}):")

        for entry in entries:
            args = entry["args"]

            # Prefer middleware description if available and detailed
            desc = entry.get("description", "")
            if desc and isinstance(desc, str) and len(desc) > 30:
                for d_line in desc.split("\n"):
                    stripped = d_line.strip()
                    if stripped and stripped.startswith(("**", "⚠", "🚨")):
                        lines.append(f"  {stripped}")
                continue

            entity = _resolve_entity_name(tool_name, args)
            ns = _resolve_namespace(tool_name, args)
            env_badge = _get_env_badge(ns)

            # Build extras
            extras: List[str] = []
            api_ver = str(args.get("apiVersion", args.get("api_version", "")))
            if api_ver:
                extras.append(api_ver)
            if "version" in args:
                extras.append(f"v{args['version']}")
            if "revision" in args:
                extras.append(f"rev {args['revision']}")
            if "target_revision" in args:
                extras.append(f"rev {args['target_revision']}")
            if "scale" in args:
                extras.append(f"replicas: {args['scale']}")
            if args.get("gracePeriodSeconds") is not None:
                gp = args["gracePeriodSeconds"]
                extras.append("⚡ FORCE DELETE" if int(gp) == 0 else f"grace: {gp}s")
            if "command" in args:
                cmd = args["command"]
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                extras.append(f"cmd: `{cmd_str[:60]}`")
            if "image" in args:
                extras.append(f"image: {args['image']}")
            if args.get("cascade") is False:
                extras.append("no cascade")
            if args.get("dry_run"):
                extras.append("dry run")

            suffix = f"  ({', '.join(extras)})" if extras else ""
            badge = f"  {env_badge}" if env_badge else ""

            # Repository tools: show URL, no namespace
            if "repository" in tool_name:
                lines.append(f"  • {entity}{suffix}")
            else:
                lines.append(f"  • {entity} → namespace: {ns}{suffix}{badge}")

        lines.append("")  # spacing between groups

    return "\n".join(lines).strip() or "Action requires approval."


# ---------------------------------------------------------------------------
# Unified Approval Component
# ---------------------------------------------------------------------------

@register_component(priority=20)
class UnifiedApprovalComponent(BaseComponent):
    """Single HITL approval card for ALL tool-call approvals.

    Handles:
      - ``HumanInTheLoopMiddleware`` interrupts (all domains)
      - Sub-agent ``request_human_input`` with phase context
      - Generic ``hitl_approval`` / ``tool_call_approval_request`` payloads

    Renders the ``hitlApprovalCard`` with **Approve / Reject** buttons.
    Labels change dynamically based on the tool name via ``TOOL_LABEL_REGISTRY``.

    Priority 20 — below ``ValuesConfirmationComponent`` (30) and
    ``PlanningApprovalComponent`` (9), so those match first for their
    specific flows.
    """

    component_type = "unified_approval"

    def can_handle(self, ctx: RenderContext) -> bool:
        if not ctx.require_user_input:
            return False

        # Values confirmation is handled by another component
        if ctx.phase == "values_confirmation":
            return False

        return self._is_approval_request(ctx.content, ctx.metadata)

    def _is_approval_request(self, content: Any, metadata: Dict[str, Any]) -> bool:
        """Detect if the HITL request is an approval request."""
        interrupt_type = metadata.get("interrupt_type", "")
        if interrupt_type in (
            "hitl_gate", "planning_review", "generation_review",
            "tool_result_review", "critical_tool_call_approval",
            "hitl_approval",
        ):
            return True

        if isinstance(content, dict):
            ctype = content.get("type", "")
            if ctype in ("tool_call_approval_request", "hitl_approval"):
                return True

            # Detect payload from request_human_input with phase
            if "question" in content and "phase" in content:
                phase = content.get("phase", "")
                if phase not in ("unknown", "generic", ""):
                    return True

            # Wrapped generic_interrupt
            if ctype == "generic_interrupt":
                data = content.get("data", {})
                if isinstance(data, dict) and "question" in data and "phase" in data:
                    if data.get("phase") not in ("unknown", "generic"):
                        return True

        # Informational messages are NOT approval requests
        content_str = str(content).lower() if content else ""
        info_keywords = [
            "i specialize in", "how can i help", "i am designed for",
            "my capabilities", "i can help with", "what would you like",
            "please provide", "could you clarify", "more information",
        ]
        for keyword in info_keywords:
            if keyword in content_str:
                return False

        return False

    def build(self, ctx: RenderContext) -> List[dict]:
        target_content = ctx.content
        content_str = str(target_content) if target_content else "Processing..."

        # ── Unwrap content from various interrupt formats ─────────
        if isinstance(target_content, dict):
            ctype = target_content.get("type", "")

            if ctype == "hitl_gate_interrupt":
                target_content = target_content.copy()
                target_content["question"] = target_content.get(
                    "summary", target_content.get("message", "Human review required"),
                )
                target_content["context"] = target_content.get("data", {})

            elif ctype == "generic_interrupt":
                data = target_content.get("data", {})
                if isinstance(data, dict) and "question" in data:
                    target_content = target_content.copy()
                    target_content["question"] = data.get("question")
                    target_content["context"] = data.get("context", "")
                    target_content["phase"] = data.get("phase", "unknown")

            elif ctype == "tool_call_approval_request":
                target_content = target_content.copy()
                tool_args = target_content.get("tool_args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}
                # Synthesize action_requests from the single tool call
                target_content["action_requests"] = [
                    {"name": target_content.get("tool_name", ""), "args": tool_args},
                ]
                target_content["question"] = target_content.get(
                    "reason", "Tool execution requires approval",
                )

            elif ctype == "hitl_approval":
                target_content = target_content.copy()
                target_content["question"] = "Human Review Required"
                action_reqs = target_content.get("action_requests", [])
                if not action_reqs and isinstance(
                    target_content.get("original_interrupt"), dict,
                ):
                    action_reqs = target_content["original_interrupt"].get(
                        "action_requests", [],
                    )
                    target_content["action_requests"] = action_reqs
                if action_reqs and isinstance(action_reqs, list):
                    target_content["context"] = _format_action_requests(action_reqs)
                    first_name = (
                        action_reqs[0].get("name", "")
                        if isinstance(action_reqs[0], dict)
                        else ""
                    )
                    target_content["phase"] = first_name or "action_approval"
                else:
                    target_content["context"] = target_content.get(
                        "summary", "Action requires approval.",
                    )
                    target_content["phase"] = "action_approval"

            elif "pending_feedback_requests" in target_content:
                target_content = target_content["pending_feedback_requests"]
            elif "pending_approval" in target_content:
                target_content = target_content["pending_approval"]
            elif "pending_tool_calls" in target_content:
                tool_calls = target_content["pending_tool_calls"]
                if tool_calls and isinstance(tool_calls, dict):
                    first_key = next(iter(tool_calls))
                    target_content = tool_calls[first_key].copy()
                    target_content["question"] = target_content.get(
                        "reason", "Tool execution requires approval",
                    )
                    target_content["context"] = f"Tool: {target_content.get('tool_name', 'unknown')}"

        # ── Extract structured fields ─────────────────────────────
        if isinstance(target_content, dict):
            question = target_content.get(
                "question",
                target_content.get("summary", target_content.get("message", content_str)),
            )
            phase = str(
                target_content.get("phase")
                or target_content.get("active_phase")
                or ctx.phase
                or "",
            )
            context_text = target_content.get("context", "")
            if isinstance(context_text, dict):
                import json
                context_text = json.dumps(context_text, indent=2)
            else:
                context_text = str(context_text)
        else:
            question = str(target_content)
            phase = ctx.phase or ""
            context_text = ""

        # ── Extract action_requests ───────────────────────────────
        action_reqs: list = []
        if isinstance(target_content, dict):
            action_reqs = target_content.get("action_requests", [])
            if not action_reqs and isinstance(
                target_content.get("original_interrupt"), dict,
            ):
                action_reqs = target_content["original_interrupt"].get(
                    "action_requests", [],
                )

        # ── Format using the unified formatter ────────────────────
        if action_reqs:
            context_text = _format_action_requests(action_reqs)
            # Derive phase from dominant tool name
            first_name = ""
            if action_reqs and isinstance(action_reqs[0], dict):
                first_name = action_reqs[0].get("name", "")
            if first_name:
                phase = first_name

        # ── Build display strings ─────────────────────────────────
        phase_display = phase.replace("_", " ").title() if phase else "Action Approval"

        # Detect environment badge from action_requests
        env_text = ""
        for req in action_reqs:
            if isinstance(req, dict):
                ns = req.get("args", {}).get("namespace", "")
                badge = _get_env_badge(ns)
                if badge:
                    env_text = f" — {badge}"
                    break

        proposed_action = f"{phase_display}{env_text}"
        if context_text.strip():
            justification = f"{question}\n\n{context_text}" if question else context_text
        else:
            justification = str(question or "")

        # ── Classify risk + extract parameters ────────────────────
        risk_level = classify_risk(phase=phase, action_requests=action_reqs)
        _, _, params = extract_parameters(action_reqs)

        surface_id = f"hitl-{phase or 'generic'}-{uuid.uuid4().hex[:8]}"

        return build_hitl_approval_surface(
            surface_id=surface_id,
            proposed_action=proposed_action,
            justification=justification,
            risk_level=risk_level,
            options=[
                {"id": "approve", "label": "✅ Approve"},
                {"id": "reject", "label": "❌ Reject"},
            ],
            action_id="hitl_response",
            phase=phase or "unknown",
            parameters=params,
        )
