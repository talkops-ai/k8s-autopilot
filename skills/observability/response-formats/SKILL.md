---
name: response-formats
description: >-
  Output formatting and visualization rules for observability subagents. Load
  this skill when presenting query results (metrics, logs, traces, alerts, or
  OTel pipeline data) to the user. Teaches when and how to use A2UI interactive
  dashboards vs Markdown tables, the Dual-Execution pattern for analytical
  questions, and consistent formatting conventions. Applies to all observability
  domains: Prometheus, Loki, Tempo, Alertmanager, and OpenTelemetry.
metadata:
  author: talkops.ai
  version: '2.0'
  scope: subagent
---

# Observability Response Formatting

## Response Mode Decision

Every query result falls into one of three modes. Choose the first that matches:

### Mode 1: Pure Visualization

**Trigger**: User asks to "show", "display", "chart", "graph", or "visualize" data
without asking an analytical question.

**Action**: Call your domain's A2UI query tool → `build_obs_a2ui` → brief summary.

### Mode 2: Dual-Execution (Analysis + Visualization)

**Trigger**: User asks a question that needs BOTH your analysis AND visual output
(e.g. "show me CPU trends and tell me how it performed", "what are the top log
errors?", "what are the top 5 CPU consuming pods?").

**Action — 3 steps in one turn:**

1. **Read**: Query with your domain's standard tool to get readable data. Analyze it.
2. **Visualize**: Call your domain's A2UI query tool for the interactive component.
3. **Render & Respond**: Call `build_obs_a2ui(kind=..., data="__USE_ARTIFACT__")`.
   Return your analytical text summary in your conversational response.

### Mode 3: Markdown-Only Fallback

**Trigger**: One of these is true:

- Data shape cannot be visualized (e.g. a single scalar value into a time-series chart)
- User explicitly requests "text only", "markdown", or "no chart"
- Your domain has no A2UI component for this data type

**Action**: Return a concise Markdown table or summary. Cap at 300 words.

## A2UI Pipeline

### Supported Domains

| Domain | A2UI Query Tool | `kind` | Standard Query Tool(s) |
|---|---|---|---|
| Prometheus | `prom_query_a2ui_chart` | `metrics` | `prom_query_instant`, `prom_query_range` |
| Loki | `loki_query_a2ui` | `logs` | `execute_logql_query` |
| Tempo | `tempo_query_a2ui` | `traces` | `search_traces`, `get_trace` |
| Alertmanager | `am_query_a2ui` | `alerts` | `am_list_alerts`, `am_get_alert_groups` |
| OpenTelemetry | `otel_query_a2ui` | `otel` | `list_collectors`, `list_instrumentations` |

### Step-by-Step (when using A2UI)

1. Validate your query using the standard tool first (short duration/limit).
2. Call the A2UI query tool natively. The `title` param MUST be a plain string.
3. You will receive: `"Data successfully fetched... call build_obs_a2ui... data='__USE_ARTIFACT__'"`
4. Call `build_obs_a2ui(kind="<your_kind>", data="__USE_ARTIFACT__")` natively.
5. The tool returns A2UI operations JSON — ignore it. End with a brief summary.

### Gotchas

- A2UI tools buffer data into `__USE_ARTIFACT__`. You **cannot** read this data.
  If you need to analyze the data AND visualize it, use Dual-Execution (Mode 2).
- Never use one domain's `kind` for another domain's data (e.g. do NOT use
  `kind="otel"` just because you want a table for Prometheus data).
- Never pass a dict/object as the `title` parameter — string only.
- Do NOT run A2UI tools inside the `eval` interpreter. Call them as native tools.
- Do NOT abandon the A2UI tool and switch to raw query just because you cannot
  read the buffered output. Rely on your verified query from step 1.

## Markdown Formatting Conventions

When falling back to Markdown:

- Lead with the result, not the process.
- Use tables for multi-row data.
- Use status icons consistently: ✅ ⚠️ ❌ 🔍 📊 📈 🚨 🔎 📋
- Always end with "What would you like to do next?"
- For errors: root cause + fix + prevention.
- Never dump raw tool output — always synthesize into human-readable format.
- Cap responses at 300 words.

## State-Modifying Operations

For write operations (creating rules, silences, ServiceMonitors, etc.):

Return: `"Completed {domain} operation: {summary}"` — no A2UI needed.
