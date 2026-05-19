# Prometheus Monitoring — Gotchas & Failure Patterns

Things the agent wouldn't know on its own. Read this when encountering unexpected
failures or before executing operations in unfamiliar environments.

---

## PromQL & Query Gotchas

- **Counter queries without `rate()`/`increase()` are BLOCKED.** The MCP server enforces
  counter semantics. Raw counter queries like `http_requests_total` return an error.
  Always wrap counters: `rate(http_requests_total[5m])`.
  Override with `allow_raw_counters=true` only when the user explicitly requests it.

- **Gauges are NOT blocked.** Metrics like `process_resident_memory_bytes` can be queried
  directly without wrapping functions.

- **Range query timeout.** Default is 30s. For queries spanning large time ranges (>24h)
  or high-cardinality metrics, set the `timeout` parameter explicitly.

- **Auto-downsampling caps at ~200 points per series.** When `step` is omitted from range
  queries, the server computes `step = (end - start) / max_points_per_series`. This protects
  the LLM context window but may hide short spikes. Use a smaller `max_points_per_series`
  or explicit `step` for higher resolution.

---

## Rule Management Gotchas

- **CRD namespace mismatch = silent duplicate.** `prom_upsert_rule_group` with
  `storage_mode: k8s_crd` and the WRONG namespace will create a **new** CRD instead
  of patching the existing one. There is no error — it succeeds silently.
  **Always** read `prom://kubernetes/prometheusrules` first to get the exact `namespace`.

- **Cross-reference is mandatory.** `prom://rules/groups` returns group names from the
  Prometheus evaluation API but does NOT expose Kubernetes metadata (CRD name, namespace,
  labels). `prom://kubernetes/prometheusrules` provides this. Use both together for CRD ops.

- **PrometheusRule selector labels.** If the Prometheus Operator's `ruleSelector` doesn't
  match the labels on your PrometheusRule CRD, the rule is silently ignored. Check
  `prom://config/runtime` for the active `ruleSelector` configuration.

- **`promtool` rate() testing is tricky.** Synthetic testing using `prom_run_rule_tests`
  for `rate()` over mock counters is notoriously unreliable due to Prometheus's rate
  extrapolation math (especially counters starting at zero). Prefer live verification
  via `prom_simulate_firing_historical` against real historical data instead.

- **Always validate before upsert.** `prom_check_rule_group` catches YAML and PromQL
  syntax errors before they reach the cluster. An invalid upsert wastes the user's HITL
  approval step.

---

## Exporter Gotchas

- **Exporter port conflicts.** Some exporters use well-known ports (`node_exporter:9100`,
  `kube-state-metrics:8080`). Installing two exporters on the same port in the same
  namespace will fail. Check `prom://topology/services` for existing services first.

- **RBAC-dependent exporters.** `kube-state-metrics` requires ServiceAccount +
  ClusterRole + ClusterRoleBinding. The `prom_install_exporter` tool creates these
  automatically, but uninstalling only removes Deployment + Service. RBAC resources
  may remain as orphans.

- **ConfigMap-dependent exporters.** `blackbox_exporter` and `snmp_exporter` require
  ConfigMap resources mounted at specific paths. The install tool creates these, but
  the user may need to customize the ConfigMap content post-install.

- **Custom service names.** `prom_install_exporter` accepts `service_name` to override
  the default. When using custom names, `prom_uninstall_exporter` must use the same
  `service_name` — it will not find resources under a different name.

- **DaemonSet exporters.** `node_exporter` and `windows_exporter` deploy as DaemonSet
  (not Deployment). The uninstall tool handles both resource types automatically.

---

## ServiceMonitor & Scrape Gotchas

- **ServiceMonitor label mismatch.** If the Prometheus Operator's `serviceMonitorSelector`
  doesn't match the labels on your ServiceMonitor, Prometheus silently ignores it.
  The `prom_apply_servicemonitor` tool auto-detects operator selector labels, but
  custom `labels` passed by the user may override this detection.

- **`file_sd` path requirements.** The JSON file path passed to `prom_manage_file_sd`
  must be (a) writable by the Prometheus process, and (b) inside a directory listed
  in `file_sd_configs` in `prometheus.yml`. The tool triggers `POST /-/reload` only
  if `backend_id` is provided AND Prometheus has `--web.enable-lifecycle` enabled.

---

## FinOps / TSDB Gotchas

- **FinOps tools generate YAML only.** `prom_plan_relabel`, `prom_create_recording_rule`,
  and `prom_configure_remote_write` return configuration YAML but do NOT apply changes
  to a running Prometheus instance. Users must manually add the YAML to their Prometheus
  configuration files. Always make this clear in the plan preview.

- **Recording rule naming convention.** Prometheus convention is `level:metric:operations`
  (e.g., `job:http_requests:rate5m`). The `prom_create_recording_rule` tool does not
  enforce this, but following it makes rules discoverable via `prom://rules/groups`.
