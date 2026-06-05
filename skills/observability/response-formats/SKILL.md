---
name: response-formats
description: >-
  Response format templates for the Observability Coordinator. Load this skill when
  formatting observability results for the user. Contains templates for: Prometheus
  metric queries, alert triage summaries, state-modifying operation results, empty
  results, Tempo trace search/summary/RED metrics, and Loki log query results.
  Use these templates to ensure consistent, polished, and human-readable output.
metadata:
  author: talkops.ai
  version: '1.0'
  scope: coordinator
---

# Observability Response Format Templates

## When to Use

Load this skill when presenting observability results to the user. These templates
ensure consistent, polished, and human-readable output across all five observability
domains.

## Templates

### Prometheus Read-Only Queries

```
**🔍 Prometheus Metrics** — `{backend_id}`

| Metric | Value | Labels |
|---|---|---|
| {metric_name} | {value} | {labels} |

*Query: `{promql_expression}` on backend `{backend_id}`.*

---
What would you like to do next?
```

### Alert Triage Summary

```
**🚨 Alert Summary** — `{backend_id}`

| Severity | Count | Top Alerts |
|---|---|---|
| 🔴 Critical | {count} | {top_alerts} |
| 🟡 Warning | {count} | {top_alerts} |
| ⚪ Info | {count} | {top_alerts} |

**Top affected services:** {services}

---
What would you like to do next?
```

### State-Modifying Operation Results

```
**{✅ Verified | ⚠️ Deployed but Unhealthy | ❌ Failed}**

- **Action**: {Installed|Created|Expired|Applied}
- **Target**: `{resource_name}`
- **System**: {Prometheus|Alertmanager|OpenTelemetry|Tempo}
- **Validation**: {Healthy | Unhealthy - specifics}
- **Query Used**: `{validation_query}`

{Diagnosis or additional context}

---
What would you like to do next?
```

### No Results Found

```
**🔍 {domain_name}** — `{backend_id}`

No {items} found. You can:
- **{suggestion_1}**
- **{suggestion_2}**

---
What would you like to do next?
```

### Tempo Trace Search Results

```
**🔎 Trace Search** — `{backend_id}`

| # | Trace ID | Service | Duration | Spans | Status |
|---|---|---|---|---|---|
| 1 | `{trace_id_short}` | {root_service} | {duration} | {span_count} | {status_icon} |

*Query: `{traceql_query}` — {result_count} traces found in the last {time_range}.*

---
What would you like to do next?
```

### Tempo Trace Summary

```
**📊 Trace Summary** — `{trace_id_short}`

**Headline**: {headline}
**Critical Path**: {critical_path_services} ({critical_path_duration})
**Error Spans**: {error_count} / {total_spans}
**Root Cause**: {suspected_root_cause}

| Service | Duration | Status | Key Attributes |
|---|---|---|---|
| {service} | {duration} | {status} | {attributes} |

---
What would you like to do next?
```

### Tempo RED Metrics

```
**📈 RED Metrics** — `{service}` on `{backend_id}`

| Metric | Current | Trend |
|---|---|---|
| 📊 Request Rate | {rate}/s | {trend_icon} |
| ❌ Error Rate | {error_rate}% | {trend_icon} |
| ⏱️ P99 Latency | {p99_ms}ms | {trend_icon} |

*Time range: {time_range}*

---
What would you like to do next?
```

### Loki Log Query Results

```
**📋 Log Results** — `{backend_id}`

| Timestamp | Service | Level | Message |
|---|---|---|---|
| {timestamp} | {service} | {level} | {message_truncated} |

*Query: `{logql_expression}` — {result_count} log lines in the last {time_range}.*

---
What would you like to do next?
```

## Formatting Guidelines

- Lead with the result, not the process.
- Use tables for multi-row data (metrics, alerts, traces, logs).
- Use status icons consistently: ✅ ⚠️ ❌ 🔍 📊 📈 🚨 🔎 📋
- Always end with "What would you like to do next?" to keep the conversation open.
- For errors: provide root cause + immediate fixes + preventive measures.
- Never dump raw tool output — always synthesize into human-readable format.
