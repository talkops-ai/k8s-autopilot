# Tempo Distributed Tracing — Workflow Reference

Detailed step-by-step procedures for all 7 Tempo observability workflows.
Each workflow includes tool chains, expected outputs, decision trees, and edge cases.

---

## Workflow 1: Error Triage (Metrics-First)

**Scenario**: A service is producing errors. Quantify error rate, find error traces, analyze root cause, and correlate.

**Entry trigger**: "errors", "error triage", "error rate", "failing", "500 errors"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_traceql_metrics_range` | Quantify error rate | `query='{ resource.service.name = "<svc>" && status = error } \| rate()'`, `since="1h"` |
| 2 | `tempo_traceql_metrics_range` | Compare with baseline rate | `query='{ resource.service.name = "<svc>" } \| rate()'`, `since="1h"` |
| 3 | `tempo_traceql_search` | Find error traces | `service="<svc>"`, `status="error"`, `since="30m"` |
| 4 | `tempo_summarize_trace` | Analyze root cause | `trace_id="<from_step_3>"` → critical path, error spans, suspected root cause |
| 5 | `tempo_find_related_traces` | Correlate with similar errors | `trace_id="<from_step_3>"`, `strategy="same_service_errors"` |
| 6 | `tempo_get_diagnostics` | Rule out backend issues | `backend_id="default"` |

### Decision Tree

| Error Rate vs Baseline | Meaning | Next Action |
|------------------------|---------|-------------|
| Error rate > 5% of total | ⚠️ Significant error spike | Search error traces → summarize |
| Error rate < 1% of total | ✅ Acceptable noise | Monitor, check if intermittent |
| Error rate = total rate | ❌ Service completely failing | Check backend health + pipeline |

### Resources

| Resource | When | Purpose |
|----------|------|---------|
| `tempo://system/backends` | Before Step 1 | Verify Tempo is reachable |
| `tempo://runbooks/error-burst` | After Step 4 | Error burst investigation runbook |

---

## Workflow 2: Latency Investigation

**Scenario**: A service is experiencing latency spikes. Confirm with P99 metrics, find slow traces, analyze critical path, and compare with normal traces.

**Entry trigger**: "slow", "latency", "P99", "latency spike", "response time"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_traceql_metrics_range` | Confirm P99 spike | `query='{ resource.service.name = "<svc>" } \| quantile_over_time(duration, 0.99)'`, `since="6h"` |
| 2 | `tempo_traceql_search` | Find slow traces | `service="<svc>"`, `min_duration_ms=<threshold>`, `since="1h"` |
| 3 | `tempo_summarize_trace` | Critical path analysis | `trace_id="<slowest_from_step_2>"` → critical path, headline, error spans |
| 4 | `tempo_traceql_search` | Find normal traces for comparison | `service="<svc>"`, `max_duration_ms=<threshold/2>`, `since="1h"`, `limit=3` |
| 5 | `tempo_compare_traces` | Diff slow vs normal | `trace_id_a="<normal>"`, `trace_id_b="<slow>"` → 5-dimensional diff |

### Interpretation

| Compare Result | Meaning | Action |
|----------------|---------|--------|
| New downstream service in slow trace | A new dependency was added | Check recent deployments |
| Same structure, longer duration | Existing service became slower | Check resource constraints |
| Critical path shows DB span | Database is bottleneck | Check connection pool, query plan |
| Time gap detected (wall-clock ≫ critical path) | Async/disjointed spans inflating trace window | Focus on critical path duration, not wall-clock |

---

## Workflow 3: Missing Traces Diagnostic

**Scenario**: Expected traces are missing. Systematically diagnose: backend health, data existence, attribute names, tenant/retention issues.

**Entry trigger**: "no traces", "missing traces", "can't find traces", "traces not showing"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_get_diagnostics` | Verify backend healthy | `backend_id="default"` |
| 2 | `tempo_get_attribute_names` | Verify data is being ingested | `backend_id="default"`, `since="1h"` |
| 3 | `tempo_traceql_search` | Broadest possible search | `backend_id="default"`, `since="24h"`, `limit=5` |
| 4 | `tempo_get_attribute_values` | Check service names | `attribute="resource.service.name"`, `since="1h"` |
| 5 | `tempo_get_backend` | Check tenant configuration | `backend_id="default"` — check `multi_tenant` |
| 6 | Read resource | Consult runbooks | `tempo://runbooks/no-traces-found`, `tempo://runbooks/cross-tenant-access` |

### Diagnostic Matrix

| Check | Tool | Passes? | Root Cause |
|-------|------|---------|------------|
| Backend healthy | `tempo_get_diagnostics` | If no → Tempo is down |
| Attributes exist | `tempo_get_attribute_names` | If no → No data ingested |
| Any traces found | `tempo_traceql_search(since="24h")` | If no → Pipeline or retention issue |
| Service name exists | `tempo_get_attribute_values` | If no → Service not instrumented |
| Service name matches | Compare query vs values | If no → Typo in service name |
| Tenant correct | `tempo_get_backend` multi_tenant check | If wrong → Wrong tenant ID |

### Edge Case: `TEMPO_REQUIRE_FILTER_OR_QUERY=true`

If the broadest search (`{ }`) is rejected, use `query="{ duration > 0ns }"` as workaround.

---

## Workflow 4: TraceQL Query Builder

**Scenario**: Build a TraceQL query from natural language intent — discover attributes, consult references, construct, and execute.

**Entry trigger**: "build query", "TraceQL", "construct query", "find traces where"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_get_attribute_names` | Discover available attributes | `scope="span"` or `scope="all"`, `since="1h"` |
| 2 | `tempo_get_attribute_values` | Explore values | `attribute="resource.service.name"`, `since="1h"` |
| 3 | `tempo_get_k8s_attribute_map` | Resolve K8s attribute names | `backend_id="default"` |
| 4 | Read resources | Consult references | `tempo://reference/traceql`, `tempo://examples/common-queries` |
| 5 | `tempo_get_query_policies` | Check guardrails | `backend_id="default"` |
| 6 | `tempo_traceql_search` | Execute constructed query | Full TraceQL query, `since="1h"` |

### TraceQL Quick Reference

| Concept | Syntax | Example |
|---------|--------|---------|
| Service filter | `resource.service.name = "value"` | `{ resource.service.name = "checkout" }` |
| Error filter | `status = error` | `{ resource.service.name = "checkout" && status = error }` |
| Duration filter | `duration > Nms` | `{ duration > 500ms }` |
| HTTP status | `span.http.status_code >= N` | `{ span.http.status_code >= 500 }` |
| Structural | `{ } >> { }` | `{ resource.service.name = "frontend" } >> { resource.service.name = "checkout" }` |
| Regex match | `name =~ "pattern"` | `{ name =~ ".*PlaceOrder.*" }` |

---

## Workflow 5: Metrics-First Triage (RED)

**Scenario**: RED analysis of a service (Rate, Errors, Duration), then pivot from metrics to traces for deep analysis.

**Entry trigger**: "RED analysis", "request rate", "error rate", "P99 duration", "metrics triage"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_traceql_metrics_range` | Request rate | `query='{ resource.service.name = "<svc>" } \| rate()'`, `since="6h"` |
| 2 | `tempo_traceql_metrics_range` | Error rate | `query='{ resource.service.name = "<svc>" && status = error } \| rate()'`, `since="6h"` |
| 3 | `tempo_traceql_metrics_range` | P99 latency | `query='{ resource.service.name = "<svc>" } \| quantile_over_time(duration, 0.99)'`, `since="6h"` |
| 4 | `tempo_traceql_metrics_instant` | Current snapshot | Same query, point-in-time |
| 5 | `tempo_get_exemplar_traces` | Pivot: metrics → traces | Extract trace IDs from metrics |
| 6 | `tempo_get_trace_from_log` | Pivot: log → trace | Extract trace ID from log line |
| 7 | `tempo_summarize_trace` | Deep dive analysis | `trace_id="<from_step_5_or_6>"` |

### RED Interpretation

| Metric | Normal | Anomalous | Next Action |
|--------|--------|-----------|-------------|
| **Rate** | Steady or gradual increase | Sudden drop or zero | Check ingestion pipeline |
| **Errors** | < 1% of total rate | Spike above baseline | Search error traces → summarize |
| **Duration** | P99 < SLO threshold | P99 spike above threshold | Find slow traces → compare |

### TraceQL Metrics Functions

| Function | Purpose | Example |
|----------|---------|---------|
| `rate()` | Spans per second | `{ resource.service.name = "checkout" } \| rate()` |
| `count_over_time()` | Total spans in window | `{ status = error } \| count_over_time()` |
| `quantile_over_time(attr, q)` | Quantile | `\| quantile_over_time(duration, 0.99)` |
| `avg_over_time(attr)` | Average | `\| avg_over_time(duration)` |
| `histogram_over_time(attr)` | Distribution | `\| histogram_over_time(duration)` |
| `\| by(attr)` | Group by | `{ status = error } \| rate() \| by(resource.service.name)` |

---

## Workflow 6: Schema Exploration

**Scenario**: First-time connection to a Tempo backend. Get a complete picture: health, topology, attributes, services, K8s mapping.

**Entry trigger**: "explore", "what's available", "first time", "discover", "schema"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_list_backends` | Discover backends | — |
| 2 | `tempo_get_backend` | Backend profile | `backend_id="default"` |
| 3 | `tempo_get_diagnostics` | Comprehensive health check | `backend_id="default"` |
| 4 | `tempo_get_attribute_names` | Attribute taxonomy | `scope="all"`, `since="1h"` |
| 5 | `tempo_get_attribute_values` | Service inventory | `attribute="resource.service.name"` |
| 6 | `tempo_get_attribute_values` | Namespace inventory | `attribute="resource.k8s.namespace.name"` |
| 7 | `tempo_get_k8s_attribute_map` | K8s attribute mapping | `backend_id="default"` |
| 8 | `tempo_get_query_policies` | Query guardrails | `backend_id="default"` |
| 9 | Read resource | Deployment overview | `tempo://deployment/overview` |

### Expected Attribute Scopes

| Scope | Expected Attributes | Assessment |
|-------|-------------------|------------|
| `resource` | `service.name`, `k8s.namespace.name`, `k8s.deployment.name`, `k8s.pod.name` | ✅ Core identity |
| `span` | `http.method`, `http.status_code`, `rpc.method`, `db.statement` | ✅ Request-level |
| `intrinsic` | `duration`, `name`, `status`, `kind`, `traceDuration`, `rootName`, `rootServiceName` | ✅ Always present |

---

## Workflow 7: Service Topology & Alerting

**Scenario**: Map service dependencies, identify high-error services, generate PromQL alerts, and manage Tempo Operator CRDs.

**Entry trigger**: "topology", "service dependencies", "alerting", "PromQL from traces", "Tempo CRD", "TempoStack"

### Tool Chain

| Step | Tool | Purpose | Key Parameters |
|------|------|---------|----------------|
| 1 | `tempo_get_service_dependencies` | Map topology | `since="1h"` |
| 2 | `tempo_get_service_dependencies` | Focused topology | `service="<svc>"`, `since="1h"` |
| 3 | `tempo_traceql_metrics_range` | Error rate per service | `query='{ status = error } \| rate() \| by(resource.service.name)'` |
| 4 | `tempo_generate_alerting_expression` | Generate alert | `service="<svc>"`, `alert_type="error_rate"`, `threshold=0.05` |
| 5 | `tempo_list_operator_crs` | List Tempo CRDs | — |
| 6 | `tempo_get_operator_cr` | Inspect CR details | `namespace`, `name`, `kind` |
| 7 | `tempo_patch_operator_cr` | Update CR (dry_run) | `retention="7d"`, `dry_run=true` |

### Alert Types

| Type | Threshold | When to Use |
|------|-----------|-------------|
| `error_rate` | Ratio (0.05 = 5%) | Service error SLO breach |
| `latency_p99` | Milliseconds | Latency SLO breach |
| `throughput_drop` | Requests/sec | Service down or degraded |

### Cross-MCP Workflow (Tempo → Prometheus)

```
tempo_generate_alerting_expression (Tempo MCP)
  → outputs yaml_snippet (PrometheusRule CRD YAML)
  → AI passes yaml_snippet to prom_upsert_rule_group (Prometheus MCP)
  → PrometheusRule CRD created in cluster
```

### Operator CRD Safety Matrix

| Operation | Tool | Destructive? | Default | HITL? |
|-----------|------|-------------|---------|-------|
| List CRs | `tempo_list_operator_crs` | No | — | No |
| Get CR detail | `tempo_get_operator_cr` | No | — | No |
| Create CR | `tempo_create_operator_cr` | **Yes** | `dry_run=true` | **Yes** |
| Patch CR | `tempo_patch_operator_cr` | **Yes** | `dry_run=true` | **Yes** |
