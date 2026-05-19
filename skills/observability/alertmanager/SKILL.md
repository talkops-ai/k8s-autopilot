---
name: alertmanager
description: >-
  Manages Alertmanager alert lifecycle and operations via MCP tools. Use when the user
  asks to triage alerts, create or manage silences, check routing configurations,
  push test alerts, audit silence changes, or review governance compliance. Also use
  when the user reports being paged incorrectly, asks about on-call status, or wants
  to mute noisy alerts — even if they don't mention Alertmanager by name.
  Do NOT use for Prometheus operations (PromQL queries, metrics, exporters, scrape
  targets, cardinality, recording/alerting rules) — those belong to the prometheus skill.
  Triggers on keywords: Alertmanager, alerts, silence, on-call, triage, routing,
  PagerDuty, Slack notification, mute, suppress, blast radius, receiver, inhibition,
  maintenance window, expire silence.
metadata:
  author: talkops.ai
  version: '2.0'
  mcp_server: Alertmanager MCP Server
compatibility: >-
  Requires Alertmanager MCP Server (server name: alertmanager-mcp-server).
  Provides 14 tools, 11 resources, and 3 guided prompts.
---

# Alertmanager Operations Skill

## When to Use

Load this skill ONLY for **state-modifying** Alertmanager operations: creating silences,
expiring silences, pushing test alerts, or extending silence durations.

Read-only queries (list alerts, on-call summary, check routing, list silences, audit log)
do NOT need this skill — the sub-agent handles those directly via the Query Fast-Path.

## Core Workflow: Explore → Plan → Implement → Verify

### 1. Explore
- If the task description provides all required parameters, skip to Planning.
- Otherwise: check `/memories/observability/operations-log.md` for recent operations context.
- Use `am://alerts/active` for a quick snapshot of active alerts.
- Use `am://silences/active` to check existing silences.
- Use `am_summarize_oncall` for on-call triage.

### 2. Plan — MANDATORY for all mutations
- Present a clear summary of intended changes.
- Call `request_human_input` with a formatted plan (include blast radius for silences).
- Wait for user approval before proceeding.

### 3. Implement
- If missing any required parameter, call `request_human_input`.
- For silence creation: MUST follow mandatory sequence below.
- For test alerts: warn user about downstream notifications.

### 4. Verify
- After silence create: `am_list_silences(state="active")` → confirm silence appears.
- After silence expire: `am_list_silences` → confirm silence moved to expired.
- After test alert: `am_list_alerts` → confirm test alert appears.
- Do NOT declare success based solely on tool stdout.

## Silence Lifecycle — MANDATORY SEQUENCE

For ANY silence creation, you MUST follow this exact sequence. **NEVER skip Step 1.**

```
Step 1: am_preview_silence       → Check blast radius (MANDATORY)
Step 2: am_validate_silence_policy → Check policy compliance
Step 3: am_create_silence        → Create the silence (after approval)
Step 4: am_list_silences         → Confirm active
Step 5: am_update_silence        → Extend if needed (optional)
Step 6: am_expire_silence        → Clean up when done
```

### Blast Radius Checks
- If `warning_flag` is raised → narrow matchers or get explicit user approval.
- If `affected_alert_count` ≥ threshold → present count to user in plan.
- Document the blast radius in the operations journal.

## Workflow Reference

For detailed step-by-step procedures with expected outputs, edge cases, and safety
guardrails, read [references/workflows.md](references/workflows.md).

| User Intent | Workflow | Key Entry Point |
|---|---|---|
| On-call triage | Alert Triage | `am_summarize_oncall` → `am_list_alerts` |
| Maintenance silence | Silence Lifecycle | `am_preview_silence` → `am_create_silence` |
| Routing audit | Routing & Notification | `am://system/config` → `am_explain_routing` |
| Integration test | Integration Testing | `am://system/receivers` → `am_push_test_alert` |
| Governance review | Governance & Compliance | `am_list_recent_changes` → `am://system/audit-log` |

## Safety Rules — MUST Follow

1. **Two-Layer HITL Model.** State-modifying operations have TWO safety layers:
   - **Layer 1 — Planning Gate**: Call `request_human_input` with a formatted plan.
   - **Layer 2 — Middleware Gate**: `HumanInTheLoopMiddleware` auto-pauses on gated tools.

2. **Preview before silence.** ALWAYS call `am_preview_silence` before creating any silence.

3. **Duration cap.** Max silence duration is 24 hours by default (`AM_MAX_SILENCE_MINUTES`).

4. **Test alerts are real.** `am_push_test_alert` fires real alerts — downstream integrations
   (Slack, PagerDuty, email) WILL receive notifications.

5. **Never blindly suppress.** Do NOT silence critical alerts without understanding the root cause.

6. **Silence cleanup.** Remind users to expire silences after maintenance windows.

7. **Missing Inputs Protection.** Never hallucinate silence IDs, alert names, or matchers.

## Gotchas

For common failure patterns, edge cases, and safety pitfalls, read
[references/gotchas.md](references/gotchas.md).

## Agentic Defaults — Apply Automatically

- **`backend_id`**: `"default"` when not specified.
- **`scope`**: `"service"` for `am_silence_alert` when not specified.
- **`duration_minutes`**: `60` when not specified for silences.
- **`created_by`**: Ask user — NEVER default this.

Always state derived defaults in the plan preview so the user can override.

## Response Format

- Lead with severity breakdown for alert triage (🔴 Critical, 🟡 Warning, ⚪ Info).
- Use tables for multi-alert or multi-silence results.
- For errors: provide root cause + immediate fixes + preventive measures.
