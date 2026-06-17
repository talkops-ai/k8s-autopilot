---
name: prometheus
description: >-
  Manages Prometheus monitoring and observability via MCP tools. Use when the user asks to
  query metrics, onboard applications to Prometheus, install/manage exporters, apply probes,
  create alerting or recording rules, analyze cardinality, troubleshoot failed targets,
  or manage scrape configurations. Also use when the user reports metrics are missing,
  targets are down, or high cardinality — even if they don't mention Prometheus by name.
  Do NOT use for Alertmanager operations (silences, routing, alert triage, on-call) —
  those belong to the alertmanager-operations skill.
  Triggers on keywords: Prometheus, PromQL, metrics, exporter, ServiceMonitor,
  scrape target, cardinality, alerting rule, recording rule, TSDB, file_sd,
  remote-write, metric endpoint, up{}, rate(), histogram.
metadata:
  author: talkops.ai
  version: '2.0'
  mcp_server: Prometheus MCP Server
compatibility: >-
  Requires Prometheus MCP Server (server name: prometheus-mcp-server).
  Provides 9 tool groups, 8 resource groups, and 3 guided prompts.
---

# Prometheus Monitoring Skill

## When to Use

Load this skill ONLY for **state-modifying** Prometheus operations: installing exporters,
applying ServiceMonitors, deleting stale ServiceMonitors, applying Probes, upserting rule groups, managing file_sd targets, or configuring
remote-write.

Read-only queries (PromQL queries, metric exploration, backend health, cardinality checks)
do NOT need this skill — the sub-agent handles those directly via the Query Fast-Path.

## Core Workflow: Explore → Plan → Implement → Verify

### 1. Explore
- If the task description provides all required parameters, skip to Planning.
- Otherwise: check `/memories/observability/operations-log.md` for recent operations context.
- Use `prom://topology/services` to discover available services.
- Use `prom://exporters/catalog` to browse available exporters.
- Use `prom://rules/groups` to discover existing rule groups.

### 2. Plan — MANDATORY for all mutations
- Present a clear summary of intended changes with blast-radius context.
- Call `request_human_input` with a formatted plan.
- Wait for user approval before proceeding.

### 3. Implement
- If missing any required parameter, call `request_human_input`.
- For exporter installs: use `prom_install_exporter` with correct namespace.
- For synthetic monitoring: use `prom_apply_probe` (do NOT use `kubectl` fallbacks, it handles labels automatically).
- For rule creation: validate syntax with `prom_check_rule_group` before upserting.
- For K8s CRD rules: always discover metadata via `prom://kubernetes/prometheusrules` first.

### 4. Verify & Diagnose (MANDATORY)
**Never declare success based solely on tool stdout.** Always run the verification query.

- After exporter install: `prom_verify_exporter` → confirm `up{}` series.
- After ServiceMonitor: `prom_query_instant(query="up{job='...'}")`.
- After applying probe: `prom_query_instant(query="probe_success")` → confirm `1`.
- After rule upsert: `prom://rules/groups` → confirm group appears.

#### Failure Diagnosis Protocol
If verification fails (e.g., `up=0` or `probe_success=0`), you MUST diagnose before returning:
1. Check `prom://topology/failed_targets`.
2. Test the endpoint directly with `prom_test_endpoint`.
3. Check exporter-specific health metrics if applicable (e.g., if scraping succeeds but target connection fails).

**Out-of-Scope Escalation**: You MUST exhaust all relevant MCP tools and resources first. If the root cause remains hidden after using MCP tools (e.g., endpoint is unreachable), you MUST NOT try to read pod logs with filesystem tools (`ls`, `grep`). Instead, explicitly return: "I have exhausted my MCP diagnostic tools. Further diagnosis requires cluster access. Please run `kubectl logs <pod-name> -n <namespace>` and `kubectl describe pod <pod-name> -n <namespace>` and share the output."

## Workflow Reference

For detailed step-by-step procedures with expected outputs, edge cases, and parameter
patterns, read [references/workflows.md](references/workflows.md).

| User Intent | Workflow | Key Entry Point |
|---|---|---|
| Onboard K8s app | K8s App Onboarding | `prom_recommend_instrumentation` |
| Wire K8s service (same namespace) | K8s App Onboarding step 5b | `prom_apply_servicemonitor(namespace=..., service_name=...)` |
| Wire K8s service (cross-namespace) | K8s App Onboarding step 5c | `prom_apply_servicemonitor(namespace="monitoring", service_name=..., target_namespace=...)` |
| Clean up broken ServiceMonitor | Retry / idempotency | `prom_delete_servicemonitor(monitor_name=..., namespace=...)` |
| Deploy exporter | Exporter Onboarding | `prom_recommend_exporter` → `prom_install_exporter` |
| Onboard VM target | VM/Legacy Onboarding | `prom_test_endpoint` → `prom_manage_file_sd` |
| Query metrics | PromQL Querying | `prom_validate_promql` → `prom_query_instant` |
| Optimize cardinality | TSDB FinOps | `prom_optimize_cardinality` → `prom_plan_relabel` |
| Create alert rule | Rule Management | `prom_draft_alert_rule` → `prom_check_rule_group` → `prom_upsert_rule_group` |
| Monitor endpoint | Synthetic Monitoring | `prom_install_exporter` → `prom_apply_probe` |
| Troubleshoot target | Target Troubleshooting | `prom://topology/failed_targets` → `prom_test_endpoint` |
| Autonomous CRD update | K8s Rule CRD Upsert | `prom://kubernetes/prometheusrules` → `prom_upsert_rule_group` |

## Safety Rules — MUST Follow

1. **Two-Layer HITL Model.** State-modifying operations have TWO safety layers:
   - **Layer 1 — Planning Gate**: Call `request_human_input` with a formatted plan.
   - **Layer 2 — Middleware Gate**: `HumanInTheLoopMiddleware` auto-pauses on gated tools.

2. **Counter enforcement.** Counters MUST use `rate()` or `increase()` unless `allow_raw_counters=true`.

3. **Validate before upsert.** Always call `prom_check_rule_group` before `prom_upsert_rule_group`.

4. **CRD metadata first.** For `storage_mode: k8s_crd`, MUST read `prom://kubernetes/prometheusrules`
   to discover CRD name, namespace, and labels before upserting.

5. **Exporter idempotency.** Check `prom://topology/services` before installing — may already exist.

6. **Monitor after write.** Verify `up{}` series after exporter/ServiceMonitor operations.

7. **Missing Inputs Protection.** Never hallucinate metric names or backend IDs.

## Synthetic Probe Workflow

When a user requests endpoint monitoring, uptime monitoring, or synthetic probes:

1. **Deploy the Prober**: Install blackbox_exporter in namespace 'monitoring' using
   `prom_install_exporter`.
2. **Apply the Probe**: Apply a Probe targeting the URL using `prom_apply_probe`,
   with `prober_url='blackbox-exporter:9115'` and `module='http_2xx'`.
3. **Verify**: Query `probe_success` metric to confirm the endpoint is healthy.

Do NOT use kubectl fallbacks for any step in this workflow.

## Gotchas

For common failure patterns, edge cases, and diagnostic fixes, read
[references/gotchas.md](references/gotchas.md).

## Agentic Defaults — Apply Automatically

- **`backend_id`**: `"default"` when not specified.
- **`namespace`**: `"monitoring"` for exporters when not specified.
- **`scrape_interval`**: `"30s"` for ServiceMonitors when not specified.
- **`storage_mode`**: `"api"` for rule upserts when not specified.

Always state derived defaults in the plan preview so the user can override.

## Response Format

- Lead with query results or operation status.
- Use tables for multi-metric or multi-target results.
- For errors: provide root cause + immediate fixes + preventive measures.

