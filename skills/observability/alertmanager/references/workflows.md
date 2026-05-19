# Alertmanager Operations — Workflow Playbook

Comprehensive step-by-step procedures for all 5 Alertmanager workflows.
Read this when executing state-modifying operations that require multi-step coordination.

---

## Workflow 1: On-Call Alert Triage

**When**: User is starting an on-call shift or needs to understand the current alert landscape.

### Steps

1. **Discover backends**
   - Read `am://system/backends`
   - Confirms connectivity and health of all Alertmanager instances

2. **Get on-call summary**
   - Call `am_summarize_oncall(backend_id="default")`
   - Returns: severity/service breakdown, total_alerts, by_severity, by_service, top_groups
   - Optional filters: `env="prod"`, `service="checkout"`, `severity="critical"`
   - Expected output format:
     ```
     🚨 On-Call Summary — 12 active alert(s)
       🔴 Critical: 3
       🟡 Warning: 7
       ⚪ Info: 2
     Top affected services: api-server (5), checkout (3), payments (2)
     ```

3. **Filter critical alerts**
   - Call `am_list_alerts(backend_id="default", severity="critical")`
   - Returns: `{alerts: [...], has_more, next_offset}`
   - Additional filters: `alertname`, `state` (active/suppressed), `receiver`, `label_filters`
   - Pagination: `limit=10, offset=10` for second page

4. **Check routing**
   - Call `am_explain_routing(backend_id="default", labels={"alertname": "HighCPU", "service": "api", "severity": "critical", "env": "prod"})`
   - Returns: `matched_route`, `receivers`, `group_labels`, `inhibited_by`, `explanation`
   - For unknown alerts: routes to default receiver (indicates potential misconfiguration)

5. **Inspect alert groups**
   - Call `am_list_alert_groups(backend_id="default")`
   - Returns groups reflecting Alertmanager's `group_by` configuration

6. **Audit default route**
   - Call `am_audit_default_route(backend_id="default")`
   - Returns: `default_receiver`, `alert_count`, `alerts`, `summary_text`
   - If no alerts hit default: "Routing looks well configured"
   - If alerts hit default: lists alert names and recommends adding specific routes
   - Optional: `limit=5` for truncated results

### Resources for Quick Triage
| Resource | Purpose |
|---|---|
| `am://alerts/active` | Fast snapshot without tool call |
| `am://alerts/groups` | Group snapshot without tool call |
| `am://system/backends` | Backend connectivity check |

---

## Workflow 2: Maintenance Silence Lifecycle

**When**: User needs to silence alerts for planned maintenance, deployments, or known-noisy alerts.

### Mandatory Sequence — NEVER Skip Step 1

1. **Preview blast radius** (MANDATORY)
   - Call `am_preview_silence(backend_id="default", matchers=[{"name": "service", "value": "checkout", "isRegex": false, "isEqual": true}])`
   - Returns: `affected_alert_count`, `affected_alerts_preview`, `would_affect_receivers`, `summary_text`, `warning_flag`
   - If `warning_flag` raised → narrow matchers or get explicit user approval
   - If `affected_alert_count` ≥ threshold → present count in plan

2. **Validate policy compliance**
   - Call `am_validate_silence_policy(backend_id="default", matchers=[...], duration_minutes=120, comment="Deploy v2.3", created_by="alice")`
   - Returns: `{allowed: true/false, violations: [...]}`
   - Common violations: severity-only matchers (too broad), empty comment, empty created_by

3. **Create silence** (MUTATES STATE)
   - Call `am_create_silence(backend_id="default", matchers=[...], duration_minutes=120, comment="Deploy v2.3", created_by="alice")`
   - Returns: `{silence_id: "...", silence: {...}}`

4. **Confirm active**
   - Call `am_list_silences(backend_id="default", state="active")`
   - Verify the new silence appears in the list

5. **Extend if needed** (optional)
   - Call `am_update_silence(backend_id="default", silence_id="<id>", add_minutes=30)`
   - Returns: `{new_silence_id: "...", silence: {...}}`
   - Alternative: explicit end time: `new_ends_at="2025-06-15T18:00:00+00:00"`
   - Can also update comment: `comment="Extended — rollback in progress"`

6. **Expire when done** (MUTATES STATE)
   - Call `am_expire_silence(backend_id="default", silence_id="<id>")`
   - Returns: `{success: true, message: "..."}`
   - Reactivates notifications for matched alerts

### Silence Scope Control (via `am_silence_alert`)

The `am_silence_alert` helper provides an LLM-friendly way to silence a specific alert:

| Scope | Matchers Used | Use Case |
|---|---|---|
| `instance` | All alert labels | Narrowest — silences exactly this alert instance |
| `service` (default) | alertname + service + env | Recommended — silences this alert type for this service |
| `env` | env only | Broadest — silences everything in the environment |

Usage variants:
- By labels: `am_silence_alert(backend_id="default", alert_labels={"alertname": "HighCPU", "service": "api", "env": "prod"}, scope="service", duration_minutes=60)`
- By fingerprint: `am_silence_alert(backend_id="default", alert_fingerprint="abc123def456", scope="service")`
- Returns: `{silence_id, silence, derived_matchers}`

### Safety Guardrails
| Guardrail | Description | Configuration |
|---|---|---|
| Duration Cap | Max silence 24h (default) | `AM_MAX_SILENCE_MINUTES` |
| Blast Radius Warning | Warns if ≥ N alerts affected | `AM_SILENCE_WARNING_THRESHOLD` |
| Duplicate Detection | Blocks equivalent active silences | Built-in |
| Policy Validation | Checks comment, creator, breadth | `am_validate_silence_policy` |
| Preview Dry-Run | Shows affected alerts before creation | `am_preview_silence` |

### Error Cases
| Error | Cause | Fix |
|---|---|---|
| "Duration 2000m exceeds 1440m cap" | Duration exceeds max | Use duration ≤ 1440 min (24h) |
| "Equivalent active silence already exists" | Duplicate silence | Skip creation — already silenced |
| "Provide 'add_minutes' or 'new_ends_at'" | Update without params | Must specify how to extend |
| "Either 'alert_fingerprint' or 'alert_labels' required" | Missing input | Provide one or the other |
| "Alert with given fingerprint not found" | Bad fingerprint | Use `am_list_alerts` to find correct fingerprint |

---

## Workflow 3: Routing & Notification Audit

**When**: User needs to understand routing configuration, verify who gets paged, or find misrouted alerts.

### Steps

1. **Inspect routing tree**
   - Read `am://system/config`
   - Returns full routing tree with nested routes, matchers, `group_by` labels, receivers, and inhibition rules (secrets redacted)

2. **List receivers**
   - Read `am://system/receivers`
   - Returns receiver names with integration types: slack, pagerduty, email, webhook, opsgenie, sns, victorops, pushover, wechat, or unknown
   - Configs are redacted for security

3. **Simulate routing for specific labels**
   - Call `am_explain_routing(backend_id="default", labels={"alertname": "HighCPU", "service": "api", "severity": "critical", "env": "prod"})`
   - Returns: `{matched_route, receivers, group_labels, inhibited_by, explanation}`
   - Test variations:
     - Critical alert → should route to PagerDuty/critical receiver
     - Warning alert → should route to Slack/warning receiver
     - Unknown alert (minimal labels) → falls to default receiver (indicates misconfiguration)

4. **Audit default route**
   - Call `am_audit_default_route(backend_id="default")`
   - Identifies alerts hitting the fallback receiver that should have specific routes

### Common Routing Questions
| Question | Tool to Use |
|---|---|
| "Who gets paged for this alert?" | `am_explain_routing` |
| "What Slack channels are configured?" | `am://system/receivers` |
| "Are any alerts falling through to default?" | `am_audit_default_route` |
| "How is the routing tree structured?" | `am://system/config` |
| "Why didn't I get notified?" | `am_explain_routing` with exact alert labels |

---

## Workflow 4: Integration Testing

**When**: User has configured a new receiver and needs to verify the full notification pipeline.

### Steps

1. **Verify receiver exists**
   - Read `am://system/receivers`
   - Confirm the target receiver (e.g., `slack-sre`) is in the list

2. **Simulate routing for test alert**
   - Call `am_explain_routing(backend_id="default", labels={"alertname": "MCPIntegrationTest", "team": "sre", "severity": "warning"})`
   - Confirm routing goes to the target receiver
   - If routes to wrong receiver → fix routing config before proceeding

3. **Push test alert** (MUTATES STATE — FIRES REAL ALERT)
   - Call `am_push_test_alert(backend_id="default", alert_labels={"alertname": "MCPIntegrationTest", "team": "sre", "severity": "warning"}, annotations={"summary": "Test alert from MCP"})`
   - Returns: `{status: "ok", result: {...}}`
   - **WARNING**: Downstream integrations (Slack, PagerDuty, email, webhooks) WILL receive notifications
   - Use descriptive `alertname` like `MCPIntegrationTest` to distinguish from real alerts
   - `alert_labels` MUST include `alertname` key

4. **Verify receipt** (manual step)
   - User confirms alert arrived in Slack/PagerDuty/email
   - Check alert list: `am_list_alerts(backend_id="default", alertname="MCPIntegrationTest")`

5. **Debug if needed**
   - If alert not received: read `am://system/config` to inspect routing tree
   - Check if routing simulation in Step 2 matched the correct receiver

---

## Workflow 5: Governance & Compliance Review

**When**: User needs to audit Alertmanager operations for compliance — who created silences, config drift, policy violations.

### Steps

1. **Export current config**
   - Read `am://system/config`
   - Returns full effective configuration (secrets redacted)
   - Use for Git diffing or compliance review

2. **Audit silence changes**
   - Call `am_list_recent_changes(backend_id="default", hours=24)`
   - Returns: `{changes: [{silence_id, action, matchers_summary, created_by, comment, timestamp}, ...], summary_text}`
   - Actions: "created" or "expired"
   - Optional: `hours=48` for longer lookback

3. **Review MCP audit log**
   - Read `am://system/audit-log`
   - Returns: `{entries: [{backend_id, operation, principal, summary, timestamp}, ...]}`
   - Shows all MCP-initiated operations

4. **Validate existing silences**
   - Call `am_validate_silence_policy(backend_id="default", matchers=[...], duration_minutes=..., comment="...", created_by="...")`
   - Checks against policy rules for each active silence

5. **Expire problematic silences** (MUTATES STATE)
   - If unauthorized or overly broad silences found:
   - Call `am_expire_silence(backend_id="default", silence_id="<id>")`
   - Reactivates notifications for affected alerts

### Governance Checklist
| Check | Tool/Resource | What to Look For |
|---|---|---|
| Config drift | `am://system/config` | Compare with Git-stored config |
| Unauthorized silences | `am_list_recent_changes` | Unknown authors, missing comments |
| Overly broad silences | `am_validate_silence_policy` | Severity-only matchers, env-only matchers |
| MCP operation history | `am://system/audit-log` | Unexpected create/expire patterns |
| Default route leakage | `am_audit_default_route` | Alerts hitting the fallback receiver |
