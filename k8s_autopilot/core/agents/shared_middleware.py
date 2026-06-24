"""
Shared coordinator middleware — cross-cutting concerns for ALL deep agents.

Extracts middleware that is **identical** across every coordinator (Observability,
App Operator, K8s Operator, Helm Operator) into a single shared module.  Each
operator's ``build_*_middleware()`` function appends the result of
``build_shared_coordinator_middleware()`` to its domain-specific stack.

Currently provides:
    1. **CodeInterpreterMiddleware** — QuickJS ``eval`` tool with read-only
       PTC allowlist.  Soft dependency: silently skipped if ``langchain-quickjs``
       is not installed.
    2. **SummarizationToolMiddleware** — proactive context compression tool
       that lets the coordinator compress history between task delegations.

Usage::

    from k8s_autopilot.core.agents.shared_middleware import (
        build_shared_coordinator_middleware,
    )

    middleware = build_domain_specific_middleware(...)
    middleware.extend(
        build_shared_coordinator_middleware(model=model, backend=backend, config=config)
    )

API References:
    - CodeInterpreterMiddleware:
      https://docs.langchain.com/oss/python/deepagents/interpreters
    - PTC (Programmatic Tool Calling):
      https://docs.langchain.com/oss/python/deepagents/interpreters#enable-ptc
    - SummarizationToolMiddleware:
      https://docs.langchain.com/oss/python/deepagents/customization#summarization
"""

from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("SharedMiddleware")

# Default PTC (Programmatic Tool Calling) allowlist.
# ONLY read-only coordinator tools.  PTC calls bypass interrupt_on (HITL)
# workflows entirely, so write/mutate tools MUST NEVER be in this list.
_DEFAULT_PTC_ALLOWLIST: List[str] = ["task", "read_file", "ls"]


# ---------------------------------------------------------------------------
# Individual middleware builders — return [] or [middleware]
# ---------------------------------------------------------------------------

def build_code_interpreter_middleware(
    *,
    ptc_allowlist: Optional[List[str]] = None,
    backend: Optional[Any] = None,
) -> List[Any]:
    """Build a ``CodeInterpreterMiddleware`` instance if available.

    Soft dependency on ``langchain-quickjs``.  Returns an empty list if the
    package is not installed, so callers can safely ``extend()`` the result.

    Args:
        ptc_allowlist: Tool names that the interpreter may call
            programmatically.  Defaults to ``["task", "read_file", "ls"]``.
        backend: If provided, passed as ``skills_backend`` so the interpreter
            can discover and auto-load skill content.

    Returns:
        A list containing 0 or 1 ``CodeInterpreterMiddleware`` instances.

    Ref: https://docs.langchain.com/oss/python/deepagents/interpreters#enable-ptc
    """
    try:
        from langchain_quickjs import CodeInterpreterMiddleware  # type: ignore[import-untyped]

        allowlist = ptc_allowlist or _DEFAULT_PTC_ALLOWLIST
        kwargs: Dict[str, Any] = {"ptc": allowlist}

        if backend:
            kwargs["skills_backend"] = backend

        mw = CodeInterpreterMiddleware(**kwargs)
        logger.info(
            "SharedMiddleware: CodeInterpreterMiddleware added "
            f"(PTC allowlist: {allowlist})"
        )
        return [mw]

    except ImportError:
        logger.debug(
            "SharedMiddleware: CodeInterpreterMiddleware skipped — "
            "install langchain-quickjs to enable"
        )
        return []


def build_summarization_middleware(
    *,
    model: Optional[str] = None,
    backend: Optional[Any] = None,
    config: Optional["Config"] = None,
) -> List[Any]:
    """Build a ``SummarizationToolMiddleware`` instance if model+backend available.

    The summarization tool lets the coordinator proactively compress its
    message history between task delegations (after ``request_chat_continue``)
    rather than waiting until the automatic 85%-threshold reactive
    summarization, which can cause mid-generation token overflow crashes.

    Args:
        model: LLM model name/identifier.  Falls back to ``config.get_llm_config()``
            if not provided.
        backend: Deep agent backend instance.
        config: Application config for fallback model resolution.

    Returns:
        A list containing 0 or 1 ``SummarizationToolMiddleware`` instances.

    Ref: https://docs.langchain.com/oss/python/deepagents/customization#summarization
    """
    resolved_model = model or (
        config.get_llm_config().get("model") if config else None
    )

    if not resolved_model or not backend:
        logger.debug(
            "SharedMiddleware: SummarizationToolMiddleware skipped — "
            "model or backend not available"
        )
        return []

    try:
        from deepagents.middleware.summarization import (
            create_summarization_tool_middleware,
        )

        mw = create_summarization_tool_middleware(resolved_model, backend)
        logger.info("SharedMiddleware: SummarizationToolMiddleware added")
        return [mw]

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "SharedMiddleware: SummarizationToolMiddleware unavailable — skipping",
            extra={"error": str(exc)},
        )
        return []


# ---------------------------------------------------------------------------
# Combined convenience builder
# ---------------------------------------------------------------------------

def build_shared_coordinator_middleware(
    *,
    model: Optional[str] = None,
    backend: Optional[Any] = None,
    config: Optional["Config"] = None,
    ptc_allowlist: Optional[List[str]] = None,
) -> List[Any]:
    """Build all shared coordinator middleware in the correct order.

    Combines CodeInterpreterMiddleware + SummarizationToolMiddleware.
    Each is independently fault-tolerant — missing dependencies or unavailable
    backends are silently skipped with a log message.

    Usage::

        middleware = build_domain_specific_middleware(config=config)
        middleware.extend(
            build_shared_coordinator_middleware(
                model=model, backend=backend, config=config,
            )
        )

    Args:
        model: LLM model name for summarization.
        backend: Deep agent backend instance.
        config: Application config for fallback resolution.
        ptc_allowlist: Override the default PTC allowlist.

    Returns:
        List of middleware instances (may be empty).
    """
    shared: List[Any] = []

    shared.extend(
        build_code_interpreter_middleware(
            ptc_allowlist=ptc_allowlist,
            backend=backend,
        )
    )

    shared.extend(
        build_summarization_middleware(
            model=model,
            backend=backend,
            config=config,
        )
    )

    return shared


# ---------------------------------------------------------------------------
# Per-domain PTC allowlists — subagent-level interpreter support
# ---------------------------------------------------------------------------
# Each list contains ONLY read-only MCP tools for a given domain.
# State-modifying (HITL-gated) tools MUST NEVER appear here because
# PTC calls execute through the interpreter bridge and bypass interrupt_on
# approval workflows entirely.
#
# The standard approach (per Deep Agents docs) is: "Treat the PTC allowlist
# as a permission boundary — expose only the tools the agent needs, and avoid
# bridging tools that can mutate data."
#
# ``read_mcp_resource`` is included in every list — it is the MCP analogue of
# ``read_file`` (which Deep Agents Code includes in PTC by default) and is
# strictly read-only.
#
# Ref: https://docs.langchain.com/oss/python/deepagents/interpreters#security-and-limits
# ---------------------------------------------------------------------------

PROMETHEUS_PTC_ALLOWLIST: List[str] = [
    # Query / exploration (read-only)
    "prom_query_instant",
    "prom_query_range",
    "prom_explore_labels",
    "prom_validate_promql",
    # Rules inspection (read-only)
    "prom_get_rule_group",
    "prom_describe_alert_rule",
    "prom_check_rule_group",
    "prom_run_rule_tests",
    # Exporter discovery (read-only)
    "prom_verify_exporter",
    "prom_recommend_exporter",
    "prom_recommend_instrumentation",
    "prom_test_endpoint",
    # Authoring / suggestion (read-only — produces YAML, does not apply)
    "prom_suggest_promql",
    "prom_draft_alert_rule",
    "prom_tune_alert_rule",
    # TSDB / cardinality analysis (read-only)
    "prom_optimize_cardinality",
    "prom_plan_relabel",
    "prom_create_recording_rule",
    # Simulation (read-only — evaluates PromQL against existing data)
    "prom_analyze_firing_history",
    "prom_simulate_firing_historical",
    "prom_simulate_firing_synthetic",
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem (standard per Deep Agents docs)
    "read_mcp_resource",
    "read_file",
    "ls",
]

ALERTMANAGER_PTC_ALLOWLIST: List[str] = [
    # Alert inspection (read-only)
    "am_list_alerts",
    "am_list_alert_groups",
    "am_summarize_oncall",
    # Silence inspection (read-only)
    "am_list_silences",
    "am_preview_silence",
    # Routing analysis (read-only)
    "am_explain_routing",
    "am_audit_default_route",
    # Governance (read-only)
    "am_validate_silence_policy",
    "am_list_recent_changes",
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]

OPENTELEMETRY_PTC_ALLOWLIST: List[str] = [
    # Discovery (read-only)
    "otel_list_collectors",
    "otel_get_collector",
    "otel_list_instrumented_services",
    "otel_verify_pipeline_health",
    "otel_list_k8s_contexts",
    "otel_lookup_instrumentation",
    # Governance / analysis (read-only)
    "otel_detect_cardinality",
    "otel_gen_drop_attribute_rules",
    "otel_analyze_ebpf_footprint",
    # Sampling inspection (read-only)
    "otel_inspect_sampling_configuration",
    "otel_inspect_spanmetrics_config",
    # Validation (read-only)
    "otel_validate_k8sattributes_order",
    "otel_check_filelog_safety",
    "otel_inspect_target_allocator_state",
    "otel_recommend_collector_topology",
    # Revert inspection (read-only — reads stored config)
    "otel_revert_collector_config",
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]

LOKI_PTC_ALLOWLIST: List[str] = [
    # All Loki tools are read-only (no HITL middleware)
    # Discovery
    "get_cluster_labels",
    "get_label_values",
    "get_active_series",
    # Structure analysis
    "get_log_patterns",
    "get_detected_fields",
    # Execution
    "execute_logql_instant",
    "execute_logql_query",
    # Safety / cost
    "get_query_stats",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]

TEMPO_PTC_ALLOWLIST: List[str] = [
    # Search / retrieval (read-only)
    "tempo_traceql_search",
    "tempo_get_trace",
    "tempo_summarize_trace",
    "tempo_find_related_traces",
    "tempo_compare_traces",
    # Schema / attributes (read-only)
    "tempo_get_attribute_names",
    "tempo_get_attribute_values",
    "tempo_get_k8s_attribute_map",
    # Discovery (read-only)
    "tempo_list_backends",
    "tempo_get_backend",
    "tempo_get_query_policies",
    # Metrics (read-only)
    "tempo_traceql_metrics_range",
    "tempo_traceql_metrics_instant",
    # Cross-pillar pivots (read-only)
    "tempo_get_exemplar_traces",
    "tempo_get_trace_from_log",
    # Topology (read-only)
    "tempo_get_service_dependencies",
    # Alerting expression generation (read-only — produces PromQL, does not apply)
    "tempo_generate_alerting_expression",
    # Diagnostics (read-only)
    "tempo_get_diagnostics",
    # Operator CRD read (read-only — excludes create/patch which are HITL-gated)
    "tempo_list_operator_crs",
    "tempo_get_operator_cr",
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]


# ---------------------------------------------------------------------------
# Helm Operator PTC allowlists
# ---------------------------------------------------------------------------
# Excludes HITL-gated tools: helm_install_chart, helm_upgrade_release,
# helm_rollback_release, helm_uninstall_release.

HELM_PTC_ALLOWLIST: List[str] = [
    # Discovery (read-only)
    "kubernetes_get_helm_releases",
    "helm_get_release_status",
    "helm_get_release_history",
    "helm_search_charts",
    "helm_get_chart_info",
    # Planning (read-only — produces plans, does not execute)
    "helm_get_installation_plan",
    "helm_dry_run_install",
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]

GITHUB_PTC_ALLOWLIST: List[str] = [
    # Read-only GitHub operations
    "get_file_contents",
    "list_files",
    "search_repositories",
    "get_repository",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]


# ---------------------------------------------------------------------------
# App Operator PTC allowlists
# ---------------------------------------------------------------------------
# Excludes HITL-gated tools: create_application, update_application,
# sync_application, delete_application, delete_project, delete_repository,
# onboard_repository_https, onboard_repository_ssh, create_project.

ARGOCD_PTC_ALLOWLIST: List[str] = [
    # Inspection (read-only)
    "list_applications",
    "get_application_details",
    "get_application_events",
    "get_application_logs",
    "get_sync_status",
    "get_application_diff",
    # Repository inspection (read-only)
    "list_repositories",
    "get_repository",
    # Project inspection (read-only)
    "list_projects",
    "get_project",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]

# Excludes HITL-gated tools: argo_delete_rollout, argo_delete_experiment,
# convert_deployment_to_rollout, convert_rollout_to_deployment,
# argo_manage_rollout_lifecycle, argo_manage_legacy_deployment,
# argo_create_rollout, argo_configure_analysis_template,
# create_stable_canary_services, argo_update_rollout.

ARGO_ROLLOUTS_PTC_ALLOWLIST: List[str] = [
    # All read-only operations are via read_mcp_resource URIs
    # (argorollout://rollouts/list, argorollout://health/summary, etc.)
    # No direct read-only tool calls beyond MCP resources.
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]

# Excludes HITL-gated tools: traefik_manage_weighted_routing,
# traefik_manage_simple_route, traefik_manage_middleware,
# traefik_nginx_migration, traefik_manage_tcp_routing,
# traefik_configure_service_affinity, traefik_generate_routing_manifest.

TRAEFIK_PTC_ALLOWLIST: List[str] = [
    # All read-only operations are via read_mcp_resource URIs
    # (traefik://traffic/routes/list, traefik://metrics/*, etc.)
    # No direct read-only tool calls beyond MCP resources.
    # Kubectl diagnostics (read-only — enforced by tool-level blocked-patterns)
    "kubectl_readonly",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]


# ---------------------------------------------------------------------------
# K8s Operator PTC allowlist
# ---------------------------------------------------------------------------
# Excludes HITL-gated tools: resources_delete, pods_delete,
# resources_create_or_update, resources_scale, pods_exec, pods_run.

K8S_CLUSTER_OPS_PTC_ALLOWLIST: List[str] = [
    # Pod inspection (read-only)
    "pods_list",
    "pods_list_in_namespace",
    "pods_get",
    "pods_log",
    "pods_top",
    # Resource inspection (read-only)
    "resources_list",
    "resources_get",
    # Namespace / events / node inspection (read-only)
    "namespaces_list",
    "events_list",
    "nodes_top",
    "nodes_stats_summary",
    "nodes_log",
    # Kubeconfig (read-only)
    "configuration_contexts_list",
    "configuration_view",
    # MCP resource + filesystem
    "read_mcp_resource",
    "read_file",
    "ls",
]


def make_subagent_interpreter_builder(
    *,
    ptc_allowlist: List[str],
    backend: Optional[Any] = None,
) -> Callable[[], Optional[Any]]:
    """Return a callable for ``extra_middleware_builders`` that creates a
    ``CodeInterpreterMiddleware`` with the given PTC allowlist.

    Returns a callable that yields ``None`` if ``langchain-quickjs``
    is not installed, so the subagent gracefully degrades.

    This follows the same pattern as Deep Agents Code's built-in PTC:
    the PTC allowlist is a permission boundary — only read-only tools
    are exposed to the QuickJS sandbox.

    Args:
        ptc_allowlist: Read-only tool names that the interpreter may call
            programmatically inside the QuickJS sandbox.
        backend: If provided, passed as ``skills_backend`` so the interpreter
            can discover and auto-load skill content.

    Returns:
        A callable suitable for ``extra_middleware_builders``.

    Ref: https://docs.langchain.com/oss/python/deepagents/interpreters#enable-ptc
    """
    def _builder() -> Optional[Any]:
        result = build_code_interpreter_middleware(
            ptc_allowlist=ptc_allowlist,
            backend=backend,
        )
        return result[0] if result else None

    return _builder
