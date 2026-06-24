# Loki Log Observability — Workflow Playbook

Comprehensive step-by-step procedures for all 7 Loki workflows.
Read this when executing multi-step guided workflows that require chaining multiple tools together.

---

## Workflow 1: Error Investigation

**When**: A service is producing errors. You need to discover the label taxonomy, find the service, validate the selector, discover log structure, pre-check query cost, fetch error logs, and quantify the error rate.

### Steps

1. **Discover available labels**
   - Call `get_cluster_labels()`
   - Returns all label names (e.g., `service_name`, `k8s_deployment_name`, `k8s_namespace_name`).
   - Expected: `{labels: [...], count: N}`

2. **Find service names**
   - Call `get_label_values(label="service_name")`
   - Returns all distinct service names. Confirm your target service exists.

3. **Validate the service selector**
   - Call `get_active_series(match='{service_name="<service>"}')`
   - Expected: `total_series` > 0, per-label cardinality, and high-cardinality warnings.
   - If `total_series` is 0: the service name is wrong or no logs are flowing.

4. **Discover log structure**
   - Call `get_detected_fields(query='{service_name="<service>"}')`
   - Returns JSON/logfmt field names, types, cardinality, and required parsers.
   - If `total_fields` is 0: logs are unstructured plain text. Use line filters (`|= "error"`) instead of `| json`.

5. **Preflight cost check**
   - Call `get_query_stats(query='{service_name="<service>"}', start="now-1h")`
   - Expected: `{streams: N, chunks: N, entries: N, bytes: N, human_bytes: "X.XX MB", exceeds_threshold: false}`
   - If `exceeds_threshold` is `true`: narrow the time range or add more specific selectors.

6. **Fetch error logs**
   - Call `execute_logql_query(query='{service_name="<service>"} |= "error"', start="now-1h", limit=100)`
   - Expected: `{result_type: "streams", streams: [...], total_lines: N, truncated: false}`

7. **Quantify error rate**
   - Call `execute_logql_instant(query='sum(rate({service_name="<service>"} |= "error" [5m]))')`
   - Returns scalar error rate (errors per second).

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `loki://system/health` | Before Step 1 | Verify Loki is reachable |
| `loki://reference/logql` | Step 6 | LogQL syntax reference |
| `loki://reference/query-templates` | Step 6 | Common error investigation query patterns |

---

## Workflow 2: Service Health Check

**When**: You want to verify that Loki is healthy and that a specific service is producing logs. This is the first thing to do when setting up monitoring or when investigating missing logs.

### Steps

1. **Check system health**
   - Read `loki://system/health`
   - Returns reachable status and label count.

2. **Verify label taxonomy**
   - Call `get_cluster_labels()`
   - Confirms that data is being ingested.

3. **Validate service has active streams**
   - Call `get_active_series(match='{service_name="<service>"}')`
   - Expected: `total_series` > 0.

4. **Check recent log volume**
   - Call `get_query_stats(query='{service_name="<service>"}', start="now-1h")`
   - Returns streams, entries, and bytes.

5. **Fetch latest logs**
   - Call `execute_logql_query(query='{service_name="<service>"}', start="now-15m", limit=10)`
   - Confirms log lines are arriving.

### Health Check Interpretation

| Outcome | Meaning | Next Action |
|---------|---------|-------------|
| Health reachable, labels present, streams active | ✅ Healthy — logs are flowing | Proceed with analysis |
| Health reachable, labels present, no streams | ⚠️ Service not found — label may be wrong | Try `get_label_values(label="service_name")` to find correct name |
| Health reachable, no labels | ⚠️ No data ingested | Check ingestion pipeline (OTel Collector → Loki) |
| Health unreachable | ❌ Loki is down | Check `LOKI_URL` and Loki deployment |

---

## Workflow 3: Log Structure Analysis

**When**: You want to understand the log format for a service — JSON, logfmt, or plain text? What fields are available? What parser should you use? Essential before building LogQL pipelines.

### Steps

1. **Validate service exists**
   - Call `get_active_series(match='{service_name="<service>"}')`
   - Expected: `total_series` > 0.

2. **Discover structured fields**
   - Call `get_detected_fields(query='{service_name="<service>"}')`
   - Returns field names, types, cardinality, and parsers.
   - Requires Loki 3.0+. If 404: Loki version doesn't support this endpoint.

3. **Discover log patterns**
   - Call `get_log_patterns(query='{service_name="<service>"}', start="now-3h")`
   - Returns recurring log shapes with frequency counts and auto-suggested `| pattern` expressions.
   - Requires Loki's pattern ingester (`pattern_ingester.enabled: true`). If 404: not enabled.

4. **Sample raw logs**
   - Call `execute_logql_query(query='{service_name="<service>"}', start="now-15m", limit=5)`
   - Visual confirmation of format.

### Parser Decision Tree

| Log Format | How to Detect | LogQL Parser |
|------------|---------------|-------------|
| JSON | Fields show `parsers: ["json"]` | `| json` |
| Logfmt | Fields show `parsers: ["logfmt"]` | `| logfmt` |
| Mixed | Fields show both parsers | Try `| json` first, fallback to `| logfmt` |
| Unstructured | No fields detected (`total_fields: 0`) | Use `| pattern "<pattern>"` from `get_log_patterns` |
| OTLP structured | Structured metadata labels present (`trace_id`, `span_id`) | Filter with `| trace_id != ""` |

---

## Workflow 4: LogQL Query Builder

**When**: You want to build a LogQL query from natural language intent — e.g., *"find all checkout logs with trace context"* or *"find gRPC timeout errors in product-catalog"*.

### Steps

1. **Discover labels**
   - Call `get_cluster_labels()` — understand what dimensions exist.

2. **Explore label values**
   - Call `get_label_values(label="service_name")` — find valid service names.

3. **Validate selector**
   - Call `get_active_series(match='{service_name="<service>"}')` — confirm streams exist.

4. **Discover filterable fields**
   - Call `get_detected_fields(query='{service_name="<service>"}')` — know what fields can be filtered on.

5. **Consult references**
   - Read `loki://reference/logql` for syntax.
   - Read `loki://reference/query-templates` for common patterns.

6. **Preflight cost check**
   - Call `get_query_stats(query='{service_name="<service>"}', start="now-1h")`
   - If cost exceeds threshold: narrow time range or add more selectors.

7. **Execute query**
   - Call `execute_logql_query(query='<constructed_query>', start="now-1h", limit=100)`

### LogQL Quick Reference

| Concept | Syntax | Example |
|---------|--------|---------|
| Stream selector | `{label="value"}` | `{service_name="checkout"}` |
| Line filter | `|= "text"`, `!= "text"` | `{service_name="product-catalog"} |= "timeout"` |
| Regex filter | `|~ "pattern"`, `!~ "pattern"` | `{k8s_namespace_name="otel-demo"} |~ "(?i)(error\|failed)"` |
| Structured metadata filter | `| key = "value"` | `{service_name="frontend-proxy"} | trace_id = "3e65bc9a..."` |
| Metric rate | `rate({...} [interval])` | `rate({service_name="checkout"} [5m])` |
| Count | `count_over_time({...} [interval])` | `count_over_time({service_name="checkout"} |= "error" [5m])` |
| Per-service grouping | `sum by (service_name)` | `sum by (service_name) (rate({k8s_namespace_name="otel-demo"} |= "error" [5m]))` |

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `loki://reference/logql` | Step 5 | Full LogQL syntax reference |
| `loki://reference/query-templates` | Step 5 | Common query patterns |
| `loki://config/guardrails` | Step 6 | Current safety thresholds |

---

## Workflow 5: Schema Exploration

**When**: You want a complete picture of the Loki cluster's label taxonomy, service inventory, cardinality health, and log formats. Recommended starting workflow when connecting to a Loki instance for the first time.

### Steps

1. **Global label taxonomy**
   - Call `get_cluster_labels()` — all label names.

2. **Service inventory**
   - Call `get_label_values(label="service_name")` — all services sending logs.

3. **Namespace inventory**
   - Call `get_label_values(label="k8s_namespace_name")` — all namespaces.

4. **Deployment inventory**
   - Call `get_label_values(label="k8s_deployment_name")` — all deployments.

5. **Cardinality analysis**
   - Call `get_active_series(match='{k8s_namespace_name="<ns>"}')` — per-label cardinality + warnings.

6. **Log structure for representative service**
   - Call `get_detected_fields(query='{service_name="<service>"}')` — fields for a representative service.

7. **Governance review**
   - Read `loki://reference/label-governance` — naming conventions, cardinality rules.

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `loki://config/guardrails` | Step 5 | Understand current thresholds |
| `loki://reference/label-governance` | Step 7 | Naming conventions, cardinality rules |
| `loki://reference/best-practices` | After exploration | Best practices for label design |

---

## Workflow 6: Incident Response

**When**: There's an active incident — services are flooding errors. No time for full discovery — skip exploration and go straight to execution.

### Steps

1. **Broad error search**
   - Call `execute_logql_query(query='{k8s_namespace_name="<ns>"} |= "error"', start="now-15m", limit=100)`
   - All errors across the namespace.

2. **Error rate by service**
   - Call `execute_logql_instant(query='sum by (service_name) (rate({k8s_namespace_name="<ns>"} |= "error" [5m]))')`
   - Error rate per service — identifies the worst offenders.

3. **Drill into worst service**
   - Call `execute_logql_query(query='{service_name="<worst_service>"} |= "error"', start="now-15m", limit=50)`
   - Detailed error logs from the most impacted service.

4. **Error patterns**
   - Call `get_log_patterns(query='{service_name="<worst_service>"}', start="now-3h")`
   - Understand recurring error shapes.

5. **Trace correlation**
   - Call `execute_logql_query(query='{service_name="<worst_service>"} | trace_id != ""', start="now-15m", limit=20)`
   - Find logs with trace context for cross-reference with Tempo.

### Incident Response Quick Reference

| Query Pattern | Purpose |
|--------------|---------|
| `{k8s_namespace_name="otel-demo"} |= "error"` | All error logs across namespace |
| `{k8s_namespace_name="otel-demo"} |~ "(?i)(panic\|fatal\|CRITICAL)"` | Critical errors only |
| `sum by (service_name) (rate({k8s_namespace_name="otel-demo"} |= "error" [5m]))` | Error rate per service |
| `topk(5, sum by (service_name) (rate({k8s_namespace_name="otel-demo"} |= "error" [5m])))` | Top 5 error-producing services |
| `{service_name="frontend-proxy"} | trace_id = "<trace_id>"` | Correlate log to trace in Tempo |

---

## Workflow 7: Performance Analysis

**When**: You want to understand the performance characteristics of a service — request rate, error rate, and trace correlation — using LogQL metric queries. This is the Loki equivalent of a RED analysis using log data.

### Steps

1. **Current request rate**
   - Call `execute_logql_instant(query='sum(rate({service_name="<service>"} [5m]))')`
   - Requests per second.

2. **Current error rate**
   - Call `execute_logql_instant(query='sum(rate({service_name="<service>"} |= "error" [5m]))')`
   - Errors per second.

3. **Request rate trend**
   - Call `execute_logql_query(query='sum(rate({service_name="<service>"} [5m]))', start="now-6h", step="5m")`
   - Rate time series.

4. **Error rate trend**
   - Call `execute_logql_query(query='sum(rate({service_name="<service>"} |= "error" [5m]))', start="now-6h", step="5m")`
   - Error time series.

5. **Per-service breakdown**
   - Call `execute_logql_query(query='sum by (service_name) (rate({k8s_namespace_name="<ns>"} [5m]))', start="now-1h", step="5m")`
   - Rate per service.

6. **Trace-correlated logs**
   - Call `execute_logql_query(query='{service_name="<service>"} | trace_id != ""', start="now-15m", limit=20)`
   - Logs with trace_id for Tempo drill-down.

### LogQL Metric Functions Reference

| Function | Purpose | Example |
|----------|---------|---------|
| `rate({...} [interval])` | Log lines per second | `rate({service_name="checkout"} [5m])` |
| `count_over_time({...} [interval])` | Total log lines in window | `count_over_time({service_name="checkout"} [5m])` |
| `sum()` | Aggregate across series | `sum(rate({service_name="checkout"} [5m]))` |
| `sum by ()` | Aggregate with grouping | `sum by (service_name) (rate({k8s_namespace_name="otel-demo"} [5m]))` |
| `topk()` | Top N series | `topk(5, sum by (service_name) (rate({k8s_namespace_name="otel-demo"} |= "error" [5m])))` |

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `loki://reference/logql` | Before Step 1 | Metric function syntax |
| `loki://config/guardrails` | Before Step 3 | Understand time window limits |
