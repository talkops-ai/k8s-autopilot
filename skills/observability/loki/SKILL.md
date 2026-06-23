---
name: loki
description: >-
  Manages Grafana Loki log observability via MCP tools. Use when the user asks to
  explore log labels, discover log structure (fields, patterns, parsers), build or execute
  LogQL queries, estimate query cost, investigate errors from logs, check service health
  via logs, or correlate logs with traces using trace_id structured metadata.
  Also use when the user reports missing logs, asks about log formats, or wants to
  understand the label taxonomy of their Loki cluster.
  Triggers on keywords: Loki, LogQL, logs, log query, labels, log patterns, log fields,
  error logs, log structure, trace_id, log rate, service_name, get_cluster_labels.
  Do NOT use for Prometheus metrics, Alertmanager routing, or OpenTelemetry pipeline
  operations — those belong to their respective skills.
metadata:
  author: talkops.ai
  version: '1.0'
  mcp_server: Loki MCP Server
compatibility: >-
  Requires Loki MCP Server (server name: loki-mcp-server).
  Provides 9 read-only tools, 8 resources, and 5 guided prompts.
  All tools are read-only — no state-modifying operations exist.
---

# Loki Log Observability Skill

## When to Use

Load this skill when executing **multi-step guided workflows** that require chaining
multiple tools together (error investigation, log structure analysis, schema exploration,
incident response, performance analysis).

Simple single-tool queries (listing labels, executing a LogQL query, checking health)
are handled directly by the sub-agent via the Query Fast-Path and do NOT require loading this SKILL file.

## Core Workflow: Discover → Explore → Query → Analyze

### 1. Discover
- If the task description provides all required parameters (label names, LogQL query), skip to Querying.
- Check `/memories/observability/operations-log.md` for recent operations context.
- Use `get_cluster_labels` to discover available label dimensions.
- Use `get_label_values` to enumerate valid values for a specific label.
- Use `get_active_series` to validate that a selector matches real streams and check cardinality.

### 2. Explore
- Use `get_detected_fields` to discover structured fields (JSON/logfmt keys, types, parsers).
- Use `get_log_patterns` to discover recurring log shapes and auto-suggested `| pattern` parsers.
- Read `loki://reference/logql` for LogQL syntax reference.
- Read `loki://reference/query-templates` for common query patterns.

### 3. Query — Cost-Aware Execution
- ALWAYS call `get_query_stats` before executing expensive queries to estimate cost (streams, chunks, bytes).
- If `exceeds_threshold` is `true`, narrow the time range or add more specific selectors.
- Call `execute_logql_query` for log range queries (returns raw markdown) or metric range queries.
- Call `loki_query_a2ui` instead of `execute_logql_query` when you want to render interactive UI log tables. **CRITICAL: When using this tool, ALWAYS append parsers and `| line_format` to your LogQL query (e.g., `| json | line_format "Method: {{.method}}, Status: {{.status}}"`) so the UI clusters and displays clean, parsed fields instead of raw access logs.**
- Call `execute_logql_instant` for point-in-time scalar answers (current error rate, log count).

### 4. Analyze & Correlate
- For trace-log correlation, filter on structured metadata: `{service_name="checkout"} | trace_id != ""`
- For error quantification, use metric queries: `sum(rate({service_name="checkout"} |= "error" [5m]))`
- For cross-service investigation, use: `sum by (service_name) (rate({k8s_namespace_name="otel-demo"} |= "error" [5m]))`

**Out-of-Scope Escalation**: You MUST exhaust all relevant MCP tools and resources first. If the root cause remains hidden after using MCP tools, explicitly return: "I have exhausted my MCP diagnostic tools. Further diagnosis requires cluster access. Please check Loki pod logs via `kubectl logs` and share the output."

## Workflow Reference

For detailed step-by-step procedures with expected outputs, edge cases, and parameter
patterns, read [references/workflows.md](references/workflows.md).

| User Intent | Workflow | Key Entry Point |
|---|---|---|
| Investigate errors | Error Investigation | `get_cluster_labels` → `get_label_values` → `get_active_series` → `get_detected_fields` → `get_query_stats` → `execute_logql_query` |
| Check service health | Service Health Check | `loki://system/health` → `get_cluster_labels` → `get_active_series` → `get_query_stats` → `execute_logql_query` |
| Analyze log structure | Log Structure Analysis | `get_active_series` → `get_detected_fields` → `get_log_patterns` → `execute_logql_query` / `loki_query_a2ui` |
| Build LogQL query | LogQL Query Builder | `get_cluster_labels` → `get_label_values` → `get_active_series` → `get_detected_fields` → `get_query_stats` → `execute_logql_query` / `loki_query_a2ui` |
| Explore label schema | Schema Exploration | `get_cluster_labels` → `get_label_values` → `get_active_series` → `get_detected_fields` |
| Incident response | Incident Response | `execute_logql_query` → `execute_logql_instant` → `get_log_patterns` |
| Performance analysis | Performance Analysis | `execute_logql_instant` → `execute_logql_query` → trace correlation |

## Safety Rules — MUST Follow

1. **Discovery-First.** Never guess label names. Always call `get_cluster_labels` and `get_label_values` first.

2. **Cost Preflight.** Always call `get_query_stats` before executing expensive queries. If `exceeds_threshold` is `true`, narrow scope.

3. **Structured Metadata Scope.** `trace_id` and `span_id` are structured metadata — they CANNOT be used inside `{...}` stream selectors. Use them after `|` as label filters.

4. **Missing Inputs Protection.** Never hallucinate label names, service names, or LogQL syntax.

5. **Time Window Safety.** Always provide a time range. Never query unbounded time ranges.

## Gotchas

For common failure patterns, edge cases, and diagnostic fixes, read
[references/gotchas.md](references/gotchas.md).

## Agentic Defaults — Apply Automatically

- **`start`**: `"now-1h"` when not specified for range queries.
- **`limit`**: `100` when not specified for log queries.
- **`step`**: `"5m"` when not specified for metric range queries.

Always state derived defaults in the query summary so the user can override.

## Response Format

- Lead with query results or operation status.
- Use tables for multi-service or multi-label results.
- For errors: provide root cause + immediate fixes + preventive measures.
- For log queries: show the most relevant log lines first, with timestamps.
