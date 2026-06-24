---
name: observability-coordinator
description: Coordinator instructions for PATH A/PATH B planning logic, write_todos usage, and step budgets.
metadata:
  author: talkops.ai
  version: '1.0'
  scope: coordinator
---

# Coordinator Planning & Orchestration

## Planning Workflow — PATH A vs PATH B

### PATH A — Full Plan (write_todos + approval gate)
Use PATH A when the request involves:
- Multiple steps with state-modifying operations (e.g., install + verify + alert rule)
- High blast radius changes (silence affecting ≥ 10 alerts, broad regex matchers)
- Domain onboarding (new exporter + ServiceMonitor + alerting rules for a service)
- Cross-pillar coordination (Prometheus + Alertmanager, OTel → Tempo verification)
- Operations in production namespaces where parameters are incomplete

PATH A sequence:
1. call `write_todos` with the full task breakdown (mark mutations as `[MUTATION]`)
2. Present the plan to the user with blast radius estimate
3. Obtain approval via `request_user_input`
4. Delegate with `[PLAN-LOCKED]` prefix
5. Validate → Log → Summarize

### PATH B — Direct Execute (no write_todos)
Use PATH B when:
- Single-step operation with fully specified parameters
- Read-only query (no state change at all)
- Named resource + explicit intent (e.g., "expire silence ID abc123")
- Short silence (< 30 min) on a narrowly scoped service

PATH B sequence:
1. Classify intent → identify sub-agent
2. Delegate once with `[READ-ONLY]` or `[STATE-MODIFYING]` prefix
3. HumanInTheLoopMiddleware gates mutation automatically
4. Log (if mutation) → Summarize

### Observability Classification Examples

**PATH A** (use write_todos + approval gate):
- "Install postgres exporter with custom config" → multi-step (install + ServiceMonitor + verify)
- "Create alerting rule for high error rate" → expression validation + upsert + simulation
- "Onboard checkout service to Prometheus" → ServiceMonitor + exporter + rule recommendation
- "Set up endpoint monitoring for https://api.example.com" → blackbox + Probe + validation
- "Provision OpenTelemetry for my Python service" → OTel onboarding + Tempo verification
- "Full incident response" → Prometheus + Alertmanager + Tempo + Loki correlation

**PATH B** (skip write_todos, delegate immediately):
- "Expire silence ABC123" → single operation, named resource
- "Query CPU metrics for the last hour" → read-only, no mutation
- "Create a silence for service=checkout for 30 minutes" → single step, explicit params
- "What alerts are firing?" → read-only triage
- "Check routing for critical alerts" → read-only introspection
- "Search for error traces in the last 1 hour" → read-only, single Tempo delegation
- "Show logs for checkout service" → read-only, single Loki delegation

## write_todos Examples (PATH A Only)

**Exporter installation:**
```
write_todos([
  {"title": "Discover namespace and existing exporters", "status": "pending"},
  {"title": "[MUTATION] Install exporter in target namespace", "status": "pending"},
  {"title": "Verify exporter is scraping (up metric = 1)", "status": "pending"},
  {"title": "Create ServiceMonitor for auto-discovery", "status": "pending"}
])
```

**Silence creation (when blast radius review needed):**
```
write_todos([
  {"title": "Preview silence blast radius (am_preview_silence)", "status": "pending"},
  {"title": "Validate silence policy compliance", "status": "pending"},
  {"title": "[MUTATION] Create silence with specified matchers", "status": "pending"},
  {"title": "Verify silence is active via am_list_silences", "status": "pending"}
])
```

**Alerting rule creation:**
```
write_todos([
  {"title": "Query current metrics to validate rule expression", "status": "pending"},
  {"title": "Draft and validate alerting rule via prom_draft_alert_rule", "status": "pending"},
  {"title": "[MUTATION] Upsert alerting/recording rule", "status": "pending"},
  {"title": "Simulate rule to verify it fires correctly", "status": "pending"},
  {"title": "Verify alert appears in Alertmanager", "status": "pending"}
])
```

**Synthetic probe setup:**
```
write_todos([
  {"title": "[MUTATION] Deploy blackbox exporter if not present", "status": "pending"},
  {"title": "[MUTATION] Apply Probe targeting endpoint", "status": "pending"},
  {"title": "Verify probe_success = 1 via PromQL query", "status": "pending"}
])
```

**OTel service onboarding:**
```
write_todos([
  {"title": "Check existing OTel collectors (otel_list_collectors)", "status": "pending"},
  {"title": "Look up language instrumentation support", "status": "pending"},
  {"title": "[MUTATION] Provision collector or patch Instrumentation CRD", "status": "pending"},
  {"title": "[MUTATION] Annotate deployment for auto-instrumentation", "status": "pending"},
  {"title": "Verify traces flowing via tempo_get_attribute_values", "status": "pending"}
])
```

## Step Budget

Max 150 steps per request. Max 5 sub-agent invocations per request.

| Request Type | Expected Sub-Agent Calls | Expected Total Steps |
|---|---|---|
| Single read-only query | 1 | 3–5 |
| Simple mutation (1 resource) | 1 | 8–12 |
| Multi-step mutation (PATH A) | 1–2 | 15–30 |
| Cross-pillar investigation | 2–4 | 30–60 |
| Full incident response (all pillars) | 4–5 | 60–120 |

If a sub-agent reports FAILED, retry at most once. After 2 failures, stop and report.
