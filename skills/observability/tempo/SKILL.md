---
name: tempo
description: >-
  Manages Grafana Tempo distributed tracing via MCP tools. Use when the user asks to
  search for traces, investigate errors or latency spikes, analyze trace critical paths,
  build TraceQL queries, run RED metrics from trace data, explore service topology,
  correlate traces with logs or metrics, run backend diagnostics, or manage Tempo Operator
  CRDs (TempoStack / TempoMonolithic).
  Also use when the user asks about exemplars, spanmetrics, cross-pillar pivots, trace
  comparison, or generating PromQL alerting expressions from trace patterns.
  Triggers on keywords: Tempo, traces, tracing, TraceQL, trace_id, trace search, latency,
  critical path, span, exemplar, spanmetrics, service topology, tempo_traceql_search,
  tempo_summarize_trace, RED metrics, P99, error triage, missing traces.
  Do NOT use for Prometheus metrics scraping, Alertmanager silence management, OpenTelemetry
  collector provisioning, or Loki log queries — those belong to their respective skills.
metadata:
  author: talkops.ai
  version: '1.0'
  mcp_server: Tempo MCP Server
compatibility: >-
  Requires Tempo MCP Server (server name: tempo-mcp-server).
  Provides 22 tools (20 read-only + 2 CRD mutations), 11 resources, and 5 guided prompts.
  CRD mutation tools (tempo_create_operator_cr, tempo_patch_operator_cr) default to dry_run=true
  and are gated by HumanInTheLoopMiddleware.
---

# Tempo Distributed Tracing Skill

## When to Use

Load this skill when executing **multi-step guided workflows** that require chaining
multiple tools together (error triage, latency investigation, missing traces diagnostic,
metrics-first RED triage, schema exploration, topology mapping).

Simple single-tool queries (searching traces, getting a trace by ID, listing backends)
are handled directly by the sub-agent via the Query Fast-Path and do NOT require loading this SKILL file.

## Core Workflow: Discover → Search → Analyze → Correlate

### 1. Discover
- If the task description provides all required parameters (backend_id, service, TraceQL query), skip to Searching.
- Check `/memories/observability/operations-log.md` for recent operations context.
- Use `tempo_list_backends` to discover available backends and their health.
- Use `tempo_get_backend` to inspect a specific backend's capabilities, version, and tenant config.
- Use `tempo_get_attribute_names` to discover available trace attributes by scope.
- Use `tempo_get_attribute_values` to enumerate valid service names, namespaces, HTTP methods.
- Use `tempo_get_k8s_attribute_map` to resolve K8s concepts to OTel attribute names.

### 2. Search
- Use `tempo_traceql_search` for TraceQL or K8s-friendly filter searches.
- Use `tempo_get_query_policies` to check guardrails before constructing queries.
- Read `tempo://reference/traceql` for TraceQL syntax reference.
- Read `tempo://examples/common-queries` for common query patterns.

### 3. Analyze
- Use `tempo_summarize_trace` for intelligent trace summarization — critical path, error detection, root cause, recommended next queries.
- Use `tempo_compare_traces` to diff two traces by ID (5-dimensional: structure, spans, timing, errors, attributes).
- Use `tempo_find_related_traces` to correlate via strategies: `same_service_errors`, `same_endpoint`, `temporal_neighbors`.

### 4. Correlate — Cross-Pillar Pivots
- **Metrics → Traces**: Use `tempo_get_exemplar_traces` to extract trace IDs from TraceQL metrics queries.
- **Logs → Traces**: Use `tempo_get_trace_from_log` to extract trace IDs from log lines and retrieve full traces.
- **Traces → Metrics**: Use `tempo_traceql_metrics_range` / `tempo_traceql_metrics_instant` for RED metrics from span data.
- **Traces → Alerts**: Use `tempo_generate_alerting_expression` to create PromQL from trace patterns → pass to `prom_upsert_rule_group`.

**Out-of-Scope Escalation**: You MUST exhaust all relevant MCP tools and resources first. If the root cause remains hidden after using MCP tools, explicitly return: "I have exhausted my MCP diagnostic tools. Further diagnosis requires cluster access. Please check Tempo pod logs via `kubectl logs` and share the output."

## Workflow Reference

For detailed step-by-step procedures with expected outputs, edge cases, and parameter
patterns, read [references/workflows.md](references/workflows.md).

| User Intent | Workflow | Key Entry Point |
|---|---|---|
| Investigate errors | Error Triage | `tempo_traceql_metrics_range` (error rate) → `tempo_traceql_search` → `tempo_summarize_trace` → `tempo_find_related_traces` → `tempo_get_diagnostics` |
| Investigate latency | Latency Investigation | `tempo_traceql_metrics_range` (P99) → `tempo_traceql_search` (slow) → `tempo_summarize_trace` → `tempo_traceql_search` (normal) → `tempo_compare_traces` |
| Missing traces | Missing Traces Diagnostic | `tempo_get_diagnostics` → `tempo_get_attribute_names` → `tempo_traceql_search` (broadest) → `tempo_get_attribute_values` → runbooks |
| Build TraceQL query | TraceQL Query Builder | `tempo_get_attribute_names` → `tempo_get_attribute_values` → `tempo_get_k8s_attribute_map` → reference → `tempo_traceql_search` |
| RED metrics analysis | Metrics-First Triage | `tempo_traceql_metrics_range` (rate/errors/P99) → `tempo_get_exemplar_traces` → `tempo_summarize_trace` |
| Explore cluster | Schema Exploration | `tempo_list_backends` → `tempo_get_backend` → `tempo_get_diagnostics` → `tempo_get_attribute_names` → `tempo_get_attribute_values` |
| Map service topology | Topology & Alerting | `tempo_get_service_dependencies` → `tempo_traceql_metrics_range` → `tempo_generate_alerting_expression` |

## Cross-MCP Workflows

### Tempo → Prometheus (Alerting Expression Handoff)
1. `tempo_generate_alerting_expression` generates PromQL + PrometheusRule YAML from trace patterns.
2. The AI reads the `next_step` instruction in the output.
3. Pass the `yaml_snippet` to `prom_upsert_rule_group` (Prometheus MCP Server).
4. PrometheusRule CRD is created in cluster, Prometheus picks up the rule.

### Tempo → Loki (Trace-Log Correlation)
1. `tempo_summarize_trace` identifies error spans with service context.
2. Use error service name to query Loki: `{service_name="<service>"} |= "error"`.
3. Or use `tempo_get_trace_from_log` to go in reverse: log line → trace.

### OTel → Tempo (Instrumentation Verification)
1. After `otel_annotate_deployment` auto-instruments a service, verify traces are flowing.
2. Use `tempo_get_attribute_values(attribute="resource.service.name")` to confirm the service appears.
3. Use `tempo_traceql_search(service="<service>")` to find sample traces.

## Operator CRD Management (State-Modifying)

Two tools are state-modifying and gated by `HumanInTheLoopMiddleware`:

| Tool | Action | Default | HITL Gated? |
|------|--------|---------|-------------|
| `tempo_create_operator_cr` | Create TempoStack/TempoMonolithic CRD | `dry_run=true` | **Yes** |
| `tempo_patch_operator_cr` | Patch existing Tempo CR fields | `dry_run=true` | **Yes** |

Always use `dry_run=true` first to preview the manifest, then execute with `dry_run=false` after approval.

## Safety Rules — MUST Follow

1. **Discovery-First.** Never guess attribute names, service names, or TraceQL syntax. Always call `tempo_get_attribute_names` and `tempo_get_attribute_values` first.

2. **Backend Awareness.** Always verify backend health via `tempo_list_backends` or `tempo_get_diagnostics` before assuming data exists.

3. **Query Guardrails.** Respect `tempo_get_query_policies` — max lookback, search limits, SPSS limits. Never send unbounded queries.

4. **Scope Awareness.** TraceQL attributes are scoped: `resource.service.name` ≠ `span.http.method` ≠ `duration` (intrinsic). Always use the correct scope prefix.

5. **Metrics-Generator Dependency.** TraceQL metrics (`tempo_traceql_metrics_range`, `tempo_traceql_metrics_instant`) and service topology (`tempo_get_service_dependencies`) require the metrics-generator with `local-blocks` processor enabled.

6. **CRD Dry-Run First.** Always use `dry_run=true` for `tempo_create_operator_cr` and `tempo_patch_operator_cr`. Never apply without review.

7. **Multi-Tenancy.** If the backend is multi-tenant, always pass the `tenant` parameter. Wrong tenant returns zero results with no error.

## Gotchas

For common failure patterns, edge cases, and diagnostic fixes, read
[references/gotchas.md](references/gotchas.md).

## Agentic Defaults — Apply Automatically

- **`since`**: `"1h"` when not specified for search and metrics queries.
- **`limit`**: `20` when not specified for trace searches.
- **`backend_id`**: `"default"` when not specified.
- **`dry_run`**: `true` for CRD mutation tools.

Always state derived defaults in the query summary so the user can override.

## Response Format

- Lead with query results or operation status.
- Use tables for multi-service or multi-metric results.
- For trace summaries: show headline, critical path, error spans, and recommended next queries.
- For errors: provide root cause + immediate fixes + preventive measures.
- For RED metrics: show rate, error rate, P99 with interpretation (normal/anomalous).
- For topology: describe service nodes and call edges with request/error rates.
