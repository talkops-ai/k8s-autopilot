# Loki Log Observability — Gotchas & Edge Cases

When querying and analyzing logs with Grafana Loki, keep these critical gotchas in mind.
This document outlines common failure patterns and their diagnostic fixes.

---

## 1. Structured Metadata vs Index Labels

**The Issue**: Loki supports both **index labels** (inside `{...}` stream selectors) and **structured metadata** fields (queryable only after `|` in the pipeline).
**Gotcha**: `trace_id` and `span_id` are ingested as **structured metadata** via OTLP. They are **NOT** index labels and **CANNOT** be used inside `{...}` stream selectors. Placing them in selectors silently returns zero results.
**The Fix**:
- ❌ Wrong: `{trace_id="3e65bc9a..."}`
- ✅ Correct: `{service_name="frontend-proxy"} | trace_id = "3e65bc9a..."`
- ✅ Correct: `{service_name="ad"} | trace_id != ""`

## 2. High-Cardinality Labels in Stream Selectors

**The Issue**: Using labels with very high cardinality (e.g., `service_instance_id` which maps to pod UIDs) inside `{...}` stream selectors causes Loki to scan an excessive number of chunk references, degrading query performance.
**Gotcha**: `get_active_series` reports per-label cardinality and issues warnings when labels exceed the `LOKI_HIGH_CARDINALITY_THRESHOLD` (default: 10,000). Labels flagged as high-cardinality should NEVER be used in stream selectors.
**The Fix**: Use high-cardinality values as pipeline filters instead:
- ❌ Wrong: `{service_instance_id="080d622b-dfea-41cc-be46-01c9f459b77a"}`
- ✅ Correct: `{service_name="checkout"} | service_instance_id = "080d622b..."`

## 3. Parser Selection: JSON vs Logfmt vs Pattern

**The Issue**: Using the wrong parser silently fails — `| json` on logfmt data produces no fields, and vice versa.
**Gotcha**: Always call `get_detected_fields` first to determine the correct parser. The `parsers` field in the response tells you exactly which parser to use.
**The Fix — Parser Decision Tree**:

| Detection Result | Parser to Use |
|-----------------|---------------|
| `parsers: ["json"]` | `| json` |
| `parsers: ["logfmt"]` | `| logfmt` |
| Both parsers listed | Try `| json` first, fallback to `| logfmt` |
| `total_fields: 0` (no fields detected) | Use `| pattern "<pattern>"` from `get_log_patterns`, or use line filters (`|= "error"`) |
| Structured metadata present (`trace_id`) | Filter with `| trace_id != ""` — no parser needed |

## 4. `get_detected_fields` Requires Loki 3.0+

**The Issue**: The `/loki/api/v1/detected_fields` endpoint is a Loki 3.0+ feature.
**Gotcha**: If you get a 404 response from `get_detected_fields`, your Loki version doesn't support it. There is no workaround — you must upgrade Loki.
**The Fix**: Fall back to `get_log_patterns` for structural discovery, or sample raw logs with `execute_logql_query(limit=5)` and visually inspect the format.

## 5. Pattern Ingester Requirement

**The Issue**: `get_log_patterns` relies on Loki's **pattern ingester**, which mines recurring structural patterns from log data.
**Gotcha**: The pattern ingester must be explicitly enabled in Loki's config (`pattern_ingester: { enabled: true }`). If not enabled, `get_log_patterns` returns a 404. Pattern data is also **ephemeral** — it typically covers only the last ~3 hours.
**The Fix**: If pattern ingester is not available, fall back to:
1. `get_detected_fields` for JSON/logfmt structure discovery.
2. Raw log sampling with `execute_logql_query(limit=5)` to visually identify patterns.

## 6. Query Cost Preflight is Essential

**The Issue**: LogQL queries scan chunks of data proportional to the time range and selector scope.
**Gotcha**: Without preflight, a broad query like `{k8s_namespace_name="production"}` over 7 days can scan gigabytes of data, overwhelming the Loki backend and the AI's context window. `execute_logql_query` includes built-in cost checking, but it may reject the query after already sending the request.
**The Fix**: Always call `get_query_stats` before heavy queries. The response includes `exceeds_threshold` (boolean) and `human_bytes` (e.g., "3.20 GB"). If `exceeds_threshold` is `true`:
- Narrow the time range (e.g., `now-1h` instead of `now-7d`)
- Add more specific selectors (e.g., add `service_name` to the selector)
- Increase `LOKI_MAX_QUERY_BYTES` if the query is genuinely needed

## 7. `service_name` vs `app` Label

**The Issue**: Different environments use different label names for the same concept.
**Gotcha**: OTel Demo environments use `service_name` (set by the OTel SDK) instead of `app`. Other environments may use `app`, `application`, or `job`. Guessing the wrong label name returns zero results.
**The Fix**: Always call `get_cluster_labels()` first to discover the actual label taxonomy, then `get_label_values(label="<discovered_label>")` to confirm valid values. Never assume a label exists.

## 8. Metric Queries Require `step` Parameter

**The Issue**: Metric queries (using `rate()`, `count_over_time()`, etc.) with `execute_logql_query` return matrix (time-series) results.
**Gotcha**: If you omit the `step` parameter, Loki uses a default step that may produce too many or too few data points. For a 6-hour range with default step, you might get hundreds of points that flood the AI's context.
**The Fix**: Always provide an explicit `step` parameter:
- For 1-hour ranges: `step="1m"` (60 points)
- For 6-hour ranges: `step="5m"` (72 points)
- For 24-hour ranges: `step="15m"` (96 points)
- For 7-day ranges: `step="1h"` (168 points)

## 9. Multi-Tenancy: `X-Scope-OrgID` Silently Scopes Results

**The Issue**: In multi-tenant Loki deployments, the `X-Scope-OrgID` header scopes all queries to a specific tenant.
**Gotcha**: If `LOKI_ORG_ID` is set to the wrong tenant, all queries return zero results with no error — just empty responses. This looks identical to "no data ingested."
**The Fix**: If queries return zero results but you know data exists:
1. Read `loki://config/backends` to check the configured `org_id`.
2. Verify the correct tenant ID with the platform team.
3. Try removing `LOKI_ORG_ID` if running in single-tenant mode.
