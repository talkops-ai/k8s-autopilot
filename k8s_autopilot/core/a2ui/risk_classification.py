"""Risk classification for HITL approval components.

Shared utility that classifies operational risk based on:
  - Tool names (destructive → HIGH, mutation → MEDIUM, read-only → LOW)
  - Phase keywords (delete, drain, uninstall → HIGH)
  - Namespace awareness (production/system namespaces → escalate one level)

Used by ``UnifiedApprovalComponent`` and ``PlanningApprovalComponent``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Sequence


class RiskLevel(StrEnum):
    """Risk classification levels for HITL operations.

    Maps directly to the ``riskLevel`` property on the UI-side
    ``HitlApprovalCard`` Lit component (low | medium | high | critical).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ── Tool-level risk classification ────────────────────────────────────────

# Destructive / irreversible operations → HIGH
_HIGH_RISK_TOOLS: frozenset[str] = frozenset({
    # K8s Operator
    "resources_delete",
    "pods_delete",
    # Helm
    "helm_uninstall_release",
    # ArgoCD
    "delete_application",
    "delete_repository",
    "delete_project",
    "prune_resources",
    # Argo Rollouts
    "argo_delete_rollout",
    "argo_delete_experiment",
    "argo_rollouts_abort",
    # Observability
    "prom_uninstall_exporter",
    "prom_delete_rule_group",
})

# Mutation / state-changing operations → MEDIUM
_MEDIUM_RISK_TOOLS: frozenset[str] = frozenset({
    # K8s Operator
    "resources_create_or_update",
    "resources_scale",
    "pods_exec",
    "pods_run",
    # Helm
    "helm_install_chart",
    "helm_upgrade_release",
    "helm_rollback_release",
    # ArgoCD
    "create_application",
    "update_application",
    "sync_application",
    "rollback_application",
    "rollback_to_revision",
    "hard_refresh",
    "cancel_deployment",
    "onboard_repository_https",
    "onboard_repository_ssh",
    "create_project",
    # Argo Rollouts
    "argo_manage_rollout_lifecycle",
    "argo_manage_legacy_deployment",
    "argo_rollouts_promote",
    "argo_rollouts_retry",
    "convert_deployment_to_rollout",
    "convert_rollout_to_deployment",
    # Traefik
    "traefik_manage_weighted_routing",
    "traefik_manage_simple_route",
    "traefik_manage_middleware",
    "traefik_nginx_migration",
    "traefik_manage_tcp_routing",
    "traefik_configure_service_affinity",
    # Observability
    "prom_apply_servicemonitor",
    "prom_apply_probe",
    "prom_install_exporter",
    "prom_upsert_rule_group",
    "prom_manage_file_sd",
    "prom_configure_remote_write",
    "am_push_test_alert",
    "am_create_silence",
    "am_update_silence",
    "am_expire_silence",
    "am_silence_alert",
})

# Phase keywords that indicate elevated risk
_HIGH_RISK_PHASES: frozenset[str] = frozenset({
    "delete", "drain", "uninstall", "destroy", "purge", "remove", "force",
})

# Namespace awareness — production or system namespaces escalate risk
_PRODUCTION_NAMESPACES: frozenset[str] = frozenset({
    "production", "prod", "prd", "live",
})

_SYSTEM_NAMESPACES: frozenset[str] = frozenset({
    "kube-system", "kube-public", "kube-node-lease",
})

# ── Risk escalation order (for namespace-based escalation) ────────────────

_ESCALATION_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.CRITICAL,
)


def _escalate(level: RiskLevel) -> RiskLevel:
    """Escalate risk by one level, capping at CRITICAL."""
    idx = _ESCALATION_ORDER.index(level)
    return _ESCALATION_ORDER[min(idx + 1, len(_ESCALATION_ORDER) - 1)]


# ── Public API ────────────────────────────────────────────────────────────

def classify_risk(
    phase: str = "",
    action_requests: Sequence[dict[str, Any]] | None = None,
) -> RiskLevel:
    """Classify the risk level for an HITL approval request.

    Classification logic (in priority order):

    1. If any tool in ``action_requests`` is in ``_HIGH_RISK_TOOLS`` → HIGH
    2. If ``phase`` contains a high-risk keyword → HIGH
    3. If any tool is in ``_MEDIUM_RISK_TOOLS`` → MEDIUM
    4. Otherwise → LOW

    Namespace escalation (applied after base classification):
    - If any action targets a production namespace → escalate one level
    - If any action targets a system namespace → escalate one level

    Args:
        phase: The workflow phase or tool name that triggered the interrupt.
        action_requests: Sequence of action_request dicts from the
            ``HumanInTheLoopMiddleware``. Each dict has ``name`` (str) and
            ``args`` (dict) keys.

    Returns:
        The classified :class:`RiskLevel`.
    """
    requests = list(action_requests or [])
    tool_names = {
        req.get("name", "")
        for req in requests
        if isinstance(req, dict)
    }

    # ── Base classification ───────────────────────────────────────────
    base_level = RiskLevel.LOW

    if tool_names & _HIGH_RISK_TOOLS:
        base_level = RiskLevel.HIGH
    elif _phase_is_high_risk(phase):
        base_level = RiskLevel.HIGH
    elif tool_names & _MEDIUM_RISK_TOOLS:
        base_level = RiskLevel.MEDIUM

    # ── Namespace-based escalation ────────────────────────────────────
    if _targets_sensitive_namespace(requests):
        base_level = _escalate(base_level)

    return base_level


def _phase_is_high_risk(phase: str) -> bool:
    """Check if the phase name contains any high-risk keywords."""
    if not phase:
        return False
    phase_lower = phase.lower()
    return any(keyword in phase_lower for keyword in _HIGH_RISK_PHASES)


def _targets_sensitive_namespace(requests: list[dict[str, Any]]) -> bool:
    """Check if any action_request targets a prod or system namespace."""
    for req in requests:
        if not isinstance(req, dict):
            continue
        args = req.get("args", {})
        if not isinstance(args, dict):
            continue
        # Check all namespace-like fields
        for ns_key in ("namespace", "destination_namespace", "dest_namespace"):
            ns = str(args.get(ns_key, "")).strip().lower()
            if ns in _PRODUCTION_NAMESPACES or ns in _SYSTEM_NAMESPACES:
                return True
    return False
