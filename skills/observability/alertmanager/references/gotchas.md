# Alertmanager Operations — Gotchas & Failure Patterns

Things the agent wouldn't know on its own. Read this when encountering unexpected
failures or before executing silence/alert operations in unfamiliar environments.

---

## Silence Gotchas

- **Preview is MANDATORY.** `am_preview_silence` must be called before every
  `am_create_silence`. Without it, you risk silencing critical alerts you didn't
  intend to suppress. This is the single most important safety check.

- **Silence update creates a new ID.** `am_update_silence` doesn't modify the
  existing silence — it expires the old one and creates a new one with a NEW
  `silence_id`. Always use the `new_silence_id` from the response for subsequent
  operations (extend, expire). The old ID becomes invalid.

- **Duration cap is server-enforced.** Default max is 1440 minutes (24 hours) via
  `AM_MAX_SILENCE_MINUTES`. Creating a silence beyond this returns an error, not
  a truncated silence. For longer maintenance windows, extend before expiration.

- **Duplicate detection is built-in.** Creating a silence with matchers identical
  to an existing active silence returns a warning and does NOT create a duplicate.
  This is a safety feature, not an error.

- **Severity-only matchers are too broad.** `am_validate_silence_policy` will flag
  silences that match only on `severity` (e.g., `severity=critical`) because they
  suppress ALL critical alerts across ALL services. Always include at least
  `alertname` or `service` in matchers.

- **Scope affects derived matchers.** When using `am_silence_alert`:
  - `instance` scope uses ALL alert labels → very narrow
  - `service` scope uses `alertname` + `service` + `env` → recommended
  - `env` scope uses `env` only → very broad (suppresses everything in env)
  
  Using `instance` scope on an alert with many labels (e.g., 15+) creates a
  silence with 15+ matchers. This is valid but hard to manage.

- **Fingerprint lookup requires active alert.** `am_silence_alert` with
  `alert_fingerprint` only works if the alert is currently active in Alertmanager.
  If the alert has resolved, the fingerprint won't be found.

---

## Alert Triage Gotchas

- **`state=suppressed` includes BOTH silenced AND inhibited alerts.** When listing
  suppressed alerts via `am_list_alerts(state="suppressed")`, the results include
  alerts that are silenced (matched by a silence) AND alerts that are inhibited
  (suppressed by another alert via inhibition rules). Check the alert's `status`
  field to distinguish between the two.

- **On-call summary is a point-in-time snapshot.** `am_summarize_oncall` captures
  the current state at the moment of the call. In high-churn environments, alerts
  may fire or resolve between the summary and subsequent tool calls.

- **Alert groups reflect Alertmanager config.** `am_list_alert_groups` returns
  groups based on the `group_by` configuration in the routing tree. Changing
  `group_by` in Alertmanager config changes the grouping without any MCP changes.

- **Pagination is essential.** For environments with many alerts, `am_list_alerts`
  returns paginated results. Check `has_more` and use `offset` to get all alerts.
  Default page size is configurable.

---

## Routing Gotchas

- **`am_explain_routing` is a simulation.** It evaluates the routing tree against
  the provided labels but does NOT actually fire an alert. Use it for "what-if"
  analysis before pushing test alerts.

- **Default route audit uses live alerts.** `am_audit_default_route` checks which
  currently-active alerts are hitting the default receiver. It doesn't predict
  future alerts — only triages what's currently firing.

- **Receiver types are inferred.** `am://system/receivers` infers integration types
  (slack, pagerduty, email, webhook) from the receiver configuration. If Alertmanager
  uses a custom or unusual receiver config, the type may show as `unknown`.

- **Secrets are always redacted.** `am://system/config` and `am://system/receivers`
  redact API keys, webhook URLs with tokens, and other sensitive values. This is
  intentional — never attempt to recover redacted values.

---

## Integration Testing Gotchas

- **Test alerts are REAL.** `am_push_test_alert` fires a genuine alert into
  Alertmanager. Downstream integrations (Slack, PagerDuty, email, webhooks) WILL
  receive notifications. Always use distinctive `alertname` values like
  `MCPIntegrationTest` so the team knows it's a test.

- **Test alerts require `alertname`.** The `alert_labels` dict MUST include an
  `alertname` key. Without it, the tool returns an error.

- **Test alerts persist until resolved.** A pushed test alert stays active in
  Alertmanager until it auto-resolves (based on `resolve_timeout` config, default
  5 minutes). You can't manually resolve it via the MCP server.

---

## Governance Gotchas

- **Audit log is MCP-scoped.** `am://system/audit-log` only shows operations
  performed through the MCP server. Direct Alertmanager API calls or `amtool`
  operations do NOT appear in this log.

- **Recent changes lookback is configurable.** `am_list_recent_changes` defaults
  to 24 hours but accepts `hours` parameter. For full compliance audits, use
  `hours=168` (7 days) or larger.

- **Config export includes inhibition rules.** `am://system/config` returns the
  full effective configuration including both the routing tree AND inhibition rules.
  For compliance review, check both — inhibition rules can silently suppress alerts
  without any explicit silence being created.
