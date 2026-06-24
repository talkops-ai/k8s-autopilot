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

## State-Modifying Workflow Details

Idempotency — Check Before Creating:
| Before creating... | First check with... | If exists... |
|---|---|---|
| Exporter | prom://topology/services or prom_verify_exporter | Skip install or update |
| ServiceMonitor | prom://topology/services | Skip — already wired |
| Stale/broken SM | prom://topology/services (if target missing) | Delete with prom_delete_servicemonitor before retrying |
| Rule Group | prom://rules/groups | Use prom_upsert_rule_group to update |
| File SD target | prom_query_instant with up{job=...} | Skip — already scraping |

Phase 1: Discovery
1. Task description has context? → proceed.
2. Else check /memories/observability/operations-log.md.
3. Unknown + list request → enumerate via resource/tool, return.
4. Unknown + targeted op → return "INCOMPLETE: missing [params]".
5. NEVER guess names. "Not found" = STOP → return INCOMPLETE.

Phase 2: Planning — call request_human_input
| Operation | question | context fields |
|---|---|---|
| Exporter Install | "Install exporter. Approve?" | 📦 Type, Namespace, K8s Resources |
| Rule Create/Update | "Rule group changes. Approve?" | 📋 Group, Backend, Rule count, Storage mode |
| ServiceMonitor (same-ns) | "Wire service to Prometheus. Approve?" | 📡 Service, Namespace, Port, Interval |
| ServiceMonitor (cross-ns) | "Wire service to Prometheus (cross-namespace). Approve?" | 📡 Service, service namespace, SM namespace, Port, Interval |
| ServiceMonitor delete | "Delete ServiceMonitor. Approve?" | 🗑 SM name, Namespace, Reason |
| File SD Add/Remove | "Modify targets. Approve?" | 📁 Targets, File path, Action |

ServiceMonitor Pre-flight Checklist:
- Confirm EXACT Kubernetes Service name (not app name, not Helm release name).
  Use prom://topology/services or ask user: `kubectl get svc -n <namespace>`.
- Confirm which namespace holds the Service. If different from monitoring namespace → use target_namespace.
- Correct call for cross-namespace: prom_apply_servicemonitor(service_name=..., namespace="monitoring", target_namespace=<ns>)
- If retrying: call prom_delete_servicemonitor first to remove the old broken SM.

WAIT for approval before proceeding.

Phase 3: Execution
Tools gated by HumanInTheLoopMiddleware. Execute with exact approved parameters.

Phase 4: Verification & Failure Diagnosis (MANDATORY)
Never declare success based on tool stdout. Always run the verification query and return
a structured health status (✅ Verified, ⚠️ Deployed but Unhealthy, or ❌ Failed).

| After... | Verify with... | If Failed |
|---|---|---|
| Exporter install | prom_verify_exporter → confirm up{} series | 1. Check prom://topology/failed_targets. 2. Run prom_test_endpoint. 3. Escalate. |
| ServiceMonitor apply | prom_query_instant(query="up{job='...'}") | 1. Check prom://topology/failed_targets. 2. Run prom_test_endpoint. 3. If no up series at all: verify correct service name and namespace (cross-namespace = target_namespace). |
| ServiceMonitor delete | prom://topology/services → confirm job gone | If job still present: confirm monitor_name and namespace were correct. |
| Rule upsert | prom://rules/groups → confirm group appears | Check namespace and ruleSelector in prom://config/runtime. |
| File SD add | prom_query_instant(query="up{job='...'}") | Same as exporter install. |

Out-of-Scope Escalation:
Exhaust all MCP tools first. If root cause remains hidden after MCP diagnostics (e.g., up=0
but prom_test_endpoint is unreachable), return:
"I have exhausted my MCP diagnostic tools. Further diagnosis requires cluster access.
Please run kubectl logs <pod-name> -n <namespace> and kubectl describe pod <pod-name> -n <namespace> and share the output."

