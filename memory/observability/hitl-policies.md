# Observability HITL Policies

> **Read-only.** This file is auto-injected into every coordinator model call. Do not modify via `edit_file`.

This file is the **authoritative declaration** of which tools require HITL approval.
It must exactly match the `interrupt_on` configs in middleware factories.

## 1. Explicit Approval Required (Gate)

The following tools trigger a `HumanInTheLoopMiddleware` interrupt requiring explicit `approve` or `reject`:

### Prometheus (6 gates — `build_prometheus_hitl_middleware`)
- `prom_install_exporter` — creates K8s Deployment + Service (+ RBAC/ConfigMap) for an exporter
- `prom_uninstall_exporter` — removes exporter Deployment/DaemonSet + Service
- `prom_apply_servicemonitor` — creates ServiceMonitor CRD to wire service to Prometheus
- `prom_upsert_rule_group` — creates or modifies alerting/recording rules (API or K8s CRD)
- `prom_manage_file_sd` — modifies file_sd targets JSON + optional Prometheus reload
- `prom_configure_remote_write` — generates remote_write YAML configuration

### Alertmanager (5 gates — `build_alertmanager_hitl_middleware`)
- `am_create_silence` — suppresses alert notifications for matching alerts
- `am_update_silence` — extends or modifies silence duration
- `am_expire_silence` — reactivates alert notifications immediately
- `am_push_test_alert` — fires a REAL alert to Alertmanager (downstream integrations receive it)
- `am_silence_alert` — quick-silence helper (creates silence from alert fingerprint/labels)

### Execution Protocol
When planning gated operations, you MUST:
1. Explain the blast radius of the change.
2. Present the exact tool input you intend to use.
3. The middleware will pause execution automatically — do not bypass.

## 2. Default Safe Operations (No Gate)

The following are **not** gated and can run autonomously:

- **All MCP resources** (`read_mcp_resource`) — read-only status, health, alerts, silences, config
- **PromQL queries** — `prom_query_instant`, `prom_query_range`, `prom_validate_promql`
- **Prometheus read-only** — `prom_explore_labels`, `prom_test_endpoint`, `prom_recommend_instrumentation`, `prom_recommend_exporter`, `prom_verify_exporter`, `prom_describe_alert_rule`, `prom_analyze_firing_history`, `prom_draft_alert_rule`, `prom_tune_alert_rule`, `prom_check_rule_group`, `prom_simulate_firing_historical`, `prom_simulate_firing_synthetic`, `prom_run_rule_tests`, `prom_optimize_cardinality`, `prom_plan_relabel`, `prom_create_recording_rule`
- **Alertmanager read-only** — `am_list_alerts`, `am_list_alert_groups`, `am_summarize_oncall`, `am_explain_routing`, `am_audit_default_route`, `am_list_silences`, `am_list_recent_changes`, `am_preview_silence`, `am_validate_silence_policy`

## 3. Plan-Aware Auto-Approval

When a plan is locked in state (`PlanLockMiddleware` detects active plan in `state["files"]`) and the coordinator delegates with `[PLAN-LOCKED]` prefix:
- The user has already reviewed and approved the exact parameters at the coordinator level.
- The `HumanInTheLoopMiddleware` still fires for all gated tools (this is the mechanical safety net).
- Future enhancement: auto-approve when tool args match the plan parameters exactly, eliminating dual-approval fatigue while maintaining deviation detection.

## 4. Tool Review Mode

For operations not in §1 but where the user context is uncertain or sensitive (e.g., production namespace silence, broad regex matchers):
- Use `request_human_input` to ask for confirmation before proceeding.
