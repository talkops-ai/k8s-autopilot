# Tempo Distributed Tracing — Gotchas & Edge Cases

When searching, analyzing, and correlating distributed traces with Grafana Tempo,
keep these critical gotchas in mind. This document outlines common failure patterns
and their diagnostic fixes, extracted from real-world testing against the OTel Demo.

---

## 0. Missing { } Selector Braces (Most Common LLM Error)

**The Issue**: TraceQL requires ALL predicate expressions to be wrapped in `{ }` selector braces. LLMs frequently omit these braces.
**Gotcha**: Bare predicates like `resource.service.name = "api" && status = error` will **always** fail with: `TraceQL validation failed: TraceQL queries must contain {} selectors or be metrics expressions`.
**The Fix**:
- ❌ Wrong: `resource.service.name = "api" && status = error`
- ❌ Wrong: `.http.method != ""`
- ❌ Wrong: `duration > 500ms`
- ❌ Wrong: `has(.http.method)`
- ✅ Correct: `{ resource.service.name = "api" && status = error }`
- ✅ Correct: `{ .http.method != "" }`
- ✅ Correct: `{ duration > 500ms }`
- ✅ Better: Use structured parameters instead of raw TraceQL:
  `tempo_traceql_search(service="api", status="error")` — server wraps automatically.

Before constructing any raw TraceQL query, read `tempo://reference/traceql` and `tempo://examples/common-queries` for correct syntax patterns.

## 1. TraceQL Scope Confusion (resource. vs span. vs intrinsic)

**The Issue**: TraceQL attributes are scoped, and using the wrong scope returns zero results with no error.
**Gotcha**: `service.name` is NOT a valid attribute — it must be `resource.service.name`. Similarly, `http.method` must be `span.http.method`. Intrinsic fields (`duration`, `status`, `name`, `kind`) have NO prefix.
**The Fix**:
- ❌ Wrong: `{ service.name = "checkout" }`
- ✅ Correct: `{ resource.service.name = "checkout" }`
- ❌ Wrong: `{ http.status_code >= 500 }`
- ✅ Correct: `{ span.http.status_code >= 500 }`
- ✅ Intrinsic (no prefix): `{ duration > 500ms }`, `{ status = error }`, `{ name =~ ".*Order.*" }`

Always call `tempo_get_attribute_names` first to discover attributes with correct scope prefixes.

## 2. Metrics-Generator Requirement

**The Issue**: TraceQL metrics functions (`rate()`, `count_over_time()`, `quantile_over_time()`, etc.) and service topology (`tempo_get_service_dependencies`) require Tempo's **metrics-generator** with the `local-blocks` processor enabled.
**Gotcha**: If metrics-generator is not configured, `tempo_traceql_metrics_range` and `tempo_traceql_metrics_instant` return errors or empty results. `tempo_get_service_dependencies` falls back to service enumeration (nodes only, no edges).
**The Fix**:
1. Run `tempo_get_diagnostics(backend_id="default")` to check backend capabilities.
2. If metrics-generator is missing, the backend cannot serve TraceQL metrics. Inform the user.
3. For topology: if `method: "service_enumeration"` is returned instead of `method: "traceql_structural"`, edge data is unavailable.

## 3. LLM Format Negotiation (Tempo 2.9+)

**The Issue**: `tempo_get_trace` attempts the `application/vnd.grafana.llm` Accept header (Tempo 2.9+) for a compact, LLM-friendly trace format.
**Gotcha**: Tempo versions below 2.9 return a `406 Not Acceptable` or ignore the header. The MCP server automatically falls back to standard OTLP JSON, but the response will be larger and consume more context tokens.
**The Fix**: This is handled automatically — no action needed. If traces are consuming too many tokens, check `tempo_get_backend` for the Tempo version. If < 2.9, set `TEMPO_LLM_FORMAT=false` to skip the negotiation attempt.

## 4. Query Guardrails Blocking Valid Queries

**The Issue**: The Tempo MCP server enforces configurable safety limits to prevent unbounded queries.
**Gotcha**: Three guardrails commonly block queries:
- `TEMPO_REQUIRE_TIME_RANGE=true` → Rejects queries without `since` parameter.
- `TEMPO_REQUIRE_FILTER_OR_QUERY=true` → Rejects the empty query `{ }` (broadest search).
- `TEMPO_MAX_SEARCH_LIMIT=100` → Silently clamps `limit` to 100 even if you request more.
**The Fix**:
- Always provide `since="1h"` (or equivalent) with every query.
- If the broadest search is needed, use `query="{ duration > 0ns }"` as a workaround for the filter requirement.
- Check `tempo_get_query_policies` to see current guardrail values.

## 5. Multi-Tenancy: Silent Zero-Results

**The Issue**: In multi-tenant Tempo deployments, the `X-Scope-OrgID` header scopes all queries to a specific tenant.
**Gotcha**: If the wrong tenant ID is configured (or no tenant is specified in a multi-tenant deployment), ALL queries return zero results with no error — identical to "no data ingested." This is the #1 cause of "missing traces" false alarms.
**The Fix**:
1. Call `tempo_get_backend(backend_id="default")` to check `multi_tenant` and `default_tenant`.
2. If `multi_tenant: true`, ensure the `tenant` parameter is passed to every tool call.
3. For cross-tenant queries, use pipe-separated values: `tenant="team-a|team-b"`.
4. If unsure about the correct tenant, check with the platform team.

## 6. False-Positive Ring Errors Behind Gateways

**The Issue**: `tempo_get_diagnostics` checks ring endpoints (`/distributor/ring`, `/ingester/ring`) to assess component health.
**Gotcha**: If `TEMPO_BASE_URL` points to a Tempo Gateway or Query-Frontend in a microservices deployment, ring endpoints return `404 Not Found`. This triggers false-positive "degraded health" findings.
**The Fix**: Set `TEMPO_DEPLOYMENT_MODE=unknown` (the default). This instructs the MCP server to gracefully skip ring checks and rely only on `/status/services` for component health. Only set `TEMPO_DEPLOYMENT_MODE=monolithic` or `microservices` if you're certain the base URL can reach all diagnostic endpoints.

## 7. Time Gap Detection in Trace Summaries

**The Issue**: `tempo_summarize_trace` reports both "wall-clock duration" (total trace window) and "critical path duration" (actual processing time).
**Gotcha**: When async or disjointed spans inflate the trace window (e.g., a Kafka consumer span starting minutes after the producer span), the wall-clock duration can be 10x the critical path duration. Treating wall-clock duration as latency is misleading.
**The Fix**: `tempo_summarize_trace` includes a `time_gap_detected` flag and explicitly reports both durations. Always use the **critical path duration** for latency analysis, not the wall-clock duration. If `time_gap_detected: true`, explain the gap to the user.

## 8. CRD Mutations Default to dry_run=true

**The Issue**: `tempo_create_operator_cr` and `tempo_patch_operator_cr` default to `dry_run=true`.
**Gotcha**: If the user says "create a TempoStack" and you execute the tool, it returns a manifest preview but does NOT create anything. The user may think the CRD was created.
**The Fix**: Always explicitly state in the response whether the operation was a dry run or a live apply:
- Dry run: "🔍 Preview generated — review the manifest below. Set `dry_run=false` to apply."
- Live apply: "✅ TempoStack created in namespace `monitoring`."

## 9. Service Topology Fallback Modes

**The Issue**: `tempo_get_service_dependencies` attempts multiple strategies to derive topology.
**Gotcha**: The tool has three fallback modes with decreasing fidelity:
1. `method: "traceql_structural"` → Full edge data (client → server). Best.
2. `method: "service_enumeration"` → Nodes only, no edges. Missing edge data.
3. `method: "attribute_lookup"` → Basic service list from attribute values. Lowest fidelity.
**The Fix**: Check the `method` field in the response. If edges are missing, the metrics-generator or structural queries aren't available. Inform the user that edge relationships require `local-blocks` enabled in the metrics-generator.

## 10. max_metrics_duration Mismatch

**The Issue**: TraceQL metrics queries have a maximum allowed time range configured in Tempo's `query_frontend.metrics.max_duration`.
**Gotcha**: If you request a metrics range exceeding this limit (e.g., `since="24h"` when max is `3h`), the query fails with a 400 error. The MCP server's `TEMPO_MAX_METRICS_DURATION` should match the backend's setting, but mismatches happen.
**The Fix**:
1. Check `tempo_get_query_policies` — the `max_metrics_duration` value shows the configured limit.
2. If your query range exceeds the limit, reduce the `since` parameter.
3. For longer-range trend analysis, use multiple shorter queries and aggregate.

## 11. Structural Queries Require Tempo 2.4+

**The Issue**: TraceQL structural operators (`>>`, `~`, `&&`) for parent-child or sibling span matching require Tempo 2.4+.
**Gotcha**: On older Tempo versions, structural queries fail silently or return parse errors. The `tempo_get_service_dependencies` tool relies on structural queries for full edge data.
**The Fix**: Check `tempo_get_backend` for the Tempo version. If < 2.4, structural queries are unavailable — fall back to non-structural filtering and accept reduced topology fidelity.

## 12. Cross-MCP Alerting: Service Name Format

**The Issue**: `tempo_generate_alerting_expression` generates PromQL referencing spanmetrics (e.g., `traces_spanmetrics_calls_total{service="checkout"}`).
**Gotcha**: The spanmetrics label name for the service depends on the OTel Collector's spanmetrics connector configuration. It could be `service`, `service_name`, or `service.name`. If the label doesn't match what Prometheus actually stores, the alerting rule silently evaluates to zero.
**The Fix**: After generating the expression, validate by querying Prometheus: `prom_query_instant(query='traces_spanmetrics_calls_total{service="checkout"}')`. If zero results, check the actual label with `prom_list_label_values(label="service")` vs `prom_list_label_values(label="service_name")`.
