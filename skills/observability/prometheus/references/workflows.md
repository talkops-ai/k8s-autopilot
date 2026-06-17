# Prometheus Monitoring — Workflow Playbook

Comprehensive step-by-step procedures for all 9 Prometheus workflows.
Read this when executing state-modifying operations that require multi-step coordination.

---

## Workflow 1: Kubernetes App Onboarding

**When**: User has a custom K8s app (Go, Java, Python, Node.js) that needs Prometheus monitoring.

### Steps

1. **Verify backend health**
   - Read `prom://system/backends/{backend_id}`
   - Confirm: connectivity, features, storage retention

2. **Choose instrumentation strategy**
   - Call `prom_recommend_instrumentation(workload_type="custom_app", language="<lang>", environment="kubernetes")`
   - Decision matrix:

   | Workload | Language | Framework | Expected Strategy |
   |---|---|---|---|
   | `custom_app` | python/go/node | — | `direct_instrumentation` |
   | `custom_app` | java | Spring Boot | `builtin_metrics` (Actuator/Micrometer) |
   | postgres/redis/nginx | — | — | `exporter` |
   | traefik/etcd/influxdb | — | — | `native_metrics` (no exporter needed) |

3. **User deploys instrumented app** (manual step)

4. **Test the /metrics endpoint**
   - Call `prom_test_endpoint(endpoint_url="http://<service>.<namespace>:<port>/metrics")`
   - Expected: `{ok: true, metrics_count: <N>, format: "prometheus"}`
   - If `ok: false`: check the `errors` array — common causes: wrong port, app not running,
     endpoint returns non-Prometheus format

5. **Apply ServiceMonitor**

   **Step 5a: Confirm the exact K8s Service name** (critical — avoid wrong selectors):
   - Read `prom://topology/services` and look for the service, OR
   - Ask the user to run `kubectl get svc -n <namespace>` and share output.
   - ⚠️ Operator-managed services often have a different name than the app (e.g. OTel Operator
     generates `otel-demo-collector-collector`, not `otel-demo-collector`).

   **Step 5b: Same-namespace case** (Service and Prometheus in the same namespace):
   ```
   prom_apply_servicemonitor(namespace="<ns>", service_name="<exact-k8s-svc-name>")
   ```

   **Step 5c: Cross-namespace case** (Service in `<ns>`, Prometheus Operator in `monitoring`):
   ```
   prom_apply_servicemonitor(
       service_name="<exact-k8s-svc-name>",
       namespace="monitoring",   # where the ServiceMonitor CRD goes
       target_namespace="<ns>", # where the K8s Service lives
   )
   ```
   This injects `spec.namespaceSelector.matchNames: [<ns>]` so Prometheus can discover the
   service across namespaces. Without this, the scrape never starts.

   **Step 5d: Cleaning up before retry** — if a previous attempt created a broken SM:
   ```
   prom_delete_servicemonitor(monitor_name="<name>-monitor", namespace="monitoring")
   # then re-apply with corrected parameters
   ```
   - Optional params: `port_name`, `path`, `interval` (default 30s), `labels`
   - Expected: returns `{applied: "ServiceMonitor/<name>", manifest_yaml: "...", notes: "..."}`

6. **Discover new metrics**
   - Call `prom_explore_labels(backend_id="default", metric_name="<metric>")`
   - Returns label keys and top values (e.g., `{method: ["GET","POST"], status: ["200","500"]}`)

7. **Verify data flows**
   - Call `prom_query_instant(backend_id="default", query="rate(<metric>[5m])")`
   - Confirms metrics are being ingested

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `prom://system/backends` | Before Step 1 | Quick overview of all backends |
| `prom://metadata/catalog` | After Step 6 | Verify new metrics appear in catalog |
| `prom://onboarding-guide` | Any time | Static onboarding reference |

---

## Workflow 2: Exporter Onboarding (Third-Party Systems)

**When**: User needs to monitor a system that doesn't natively expose Prometheus metrics.

### Steps

1. **Browse exporter catalog**
   - Read `prom://exporters/catalog`
   - Returns 19 exporters with type, description, default_port, image, scope

2. **Get recommendation**
   - Call `prom_recommend_exporter(service_type="<system>")`
   - Returns matching exporters with ports and notes

3. **Install exporter** (MUTATES CLUSTER)
   - Call `prom_install_exporter(exporter_type="<type>", namespace="monitoring")`
   - Creates: Deployment + Service (+ RBAC/ConfigMap if needed)
   - Expected: `{applied_resources: [...], manifest_yaml: "..."}`
   - Optional: `service_name` override for custom naming
   - **RBAC variants**: `kube-state-metrics` creates ServiceAccount + ClusterRole + ClusterRoleBinding
   - **ConfigMap variants**: `blackbox_exporter`, `snmp_exporter` create ConfigMap mounted at /config
   - **DaemonSet variants**: `node_exporter`, `windows_exporter` deploy as DaemonSet

4. **Test endpoint**
   - Call `prom_test_endpoint(endpoint_url="http://<exporter>.<ns>:<port>/metrics")`
   - Common ports: postgres=9187, redis=9121, nginx=9113, kafka=9308, node=9100

5. **Apply ServiceMonitor**
   - Call `prom_apply_servicemonitor(namespace="<ns>", service_name="<exporter>")`

6. **Verify end-to-end**
   - Call `prom_verify_exporter(backend_id="default", endpoint_url="...", job="<job>", verify_timeout=90)`
   - Checks: endpoint health + `up{}` series in Prometheus
   - Expected: `{endpoint_check: {...}, up_series_found: true/false, errors: [...]}`

### Uninstall
- Call `prom_uninstall_exporter(exporter_type="<type>", namespace="<ns>")`
- Removes: Deployment/DaemonSet + Service (tries all resource types)
- With custom name: must pass same `service_name` used during install

---

## Workflow 3: VM/Legacy Onboarding

**When**: User needs to monitor an app on a VM or bare-metal server (not Kubernetes).

### Steps

1. **Recommend strategy**
   - Call `prom_recommend_instrumentation(workload_type="custom_app", language="<lang>", environment="vm")`
   - Returns VM-specific guidance

2. **Deploy on VM** (manual — install exporter binary or instrumented app)

3. **Test endpoint**
   - Call `prom_test_endpoint(endpoint_url="http://<IP>:<port>/metrics")`

4. **Add to file_sd**
   - Call `prom_manage_file_sd(file_sd_path="<path>", targets=["<IP>:<port>"], target_labels={"job": "<name>"}, backend_id="default")`
   - Sub-actions: `add` (default) appends targets, `remove` removes matching targets
   - Triggers `POST /-/reload` if `backend_id` is provided and Prometheus has `--web.enable-lifecycle`
   - **Important**: path must be writable by Prometheus and inside `file_sd_configs` directory

5. **Verify**
   - Call `prom_query_instant(backend_id="default", query="up{job='<name>'}")`
   - Confirms target is up and being scraped

---

## Workflow 4: PromQL Querying

**When**: User needs to query Prometheus metrics safely.

### Steps

1. **Discover service metrics**
   - Read `prom://topology/services/{job}/metrics`
   - Returns all metrics emitted by the service with `type` and `help` text

2. **Explore metric labels**
   - Call `prom_explore_labels(backend_id="default", metric_name="<metric>")`
   - Returns label keys and top values to construct accurate queries

3. **Validate query syntax**
   - Call `prom_validate_promql(backend_id="default", query="<query>")`
   - Returns `{valid: true/false, error: "..."}`

4. **Run instant query** (point-in-time)
   - Call `prom_query_instant(backend_id="default", query="<query>")`

5. **Run range query** (time series)
   - Call `prom_query_range(backend_id="default", query="<query>", start=<unix>, end=<unix>)`
   - Auto-computes `step` when omitted: `step = (end - start) / max_points_per_series`
   - Default: ~200 points per series (protects LLM context window)
   - Custom resolution: pass `step="60s"` or `max_points_per_series=50`

6. **Calculate latency (histograms)**
   - Average duration: `sum(rate(duration_sum[5m])) / sum(rate(duration_count[5m]))`

### Safety Guardrails
| Guardrail | Description | Override |
|---|---|---|
| Counter enforcement | Counters must use `rate()`/`increase()` | `allow_raw_counters=true` |
| Auto-downsampling | Range queries capped at ~200 pts/series | `max_points_per_series` param |
| Query validation | Syntax checked before execution | Use `action=validate` first |
| Timeout | Default 30s query timeout | `timeout` param |

### Error Cases
| Error | Cause | Fix |
|---|---|---|
| "requires 'query' parameter" | Missing query | Pass `query=` parameter |
| "requires 'start' and 'end'" | Range query without bounds | Pass `start=` and `end=` |
| "requires 'metric_name'" | Label exploration without metric | Pass `metric_name=` |
| Counter blocked error | Raw counter without rate() | Wrap in `rate()` or set `allow_raw_counters=true` |

---

## Workflow 5: TSDB FinOps & Cardinality Optimization

**When**: Prometheus storage costs are growing or cardinality is too high.

**Important**: All FinOps generation tools produce YAML configs. They do NOT apply
changes to a running Prometheus instance. Users must manually add YAML to their config.

### Steps

1. **Get cardinality overview**
   - Read `prom://tsdb/cardinality`
   - Returns: `{overview: {total_series: <N>}, top_cardinality_metrics: [...]}`

2. **Analyze hotspots**
   - Call `prom_optimize_cardinality(backend_id="default", top_n=10)`
   - Returns recommendations with severity (critical/high/medium)
   - Target specific metric: add `metric_name="<metric>"`

3. **Investigate labels**
   - Call `prom_explore_labels(backend_id="default", metric_name="<hot_metric>")`
   - Find high-cardinality label dimensions

4. **Generate relabel config** (YAML output only)
   - Drop labels: `prom_plan_relabel(backend_id="default", labels_to_drop=["pod_id", "container_id"])`
     → generates `action: labeldrop` rules
   - Keep labels only: `prom_plan_relabel(backend_id="default", labels_to_keep=["job", "namespace"])`
     → generates `action: labelkeep` rule
   - Drop entire metric: `prom_plan_relabel(backend_id="default", metric_name="kubelet_runtime_operations_total")`
     → generates `action: drop` with `source_labels: [__name__]`
   - Scoped drop: `prom_plan_relabel(backend_id="default", metric_name="http_requests_total", labels_to_drop=["user_id"])`

5. **Create recording rule** (YAML output only)
   - Call `prom_create_recording_rule(backend_id="default", rule_name="job:http_requests:rate5m", rule_expr="sum by (job) (rate(http_requests_total[5m]))")`
   - Optional: `rule_labels={"aggregation": "env"}`, `rule_interval="5m"`
   - Expected: valid Prometheus rule group YAML

6. **Configure remote-write** (YAML output only)
   - Call `prom_configure_remote_write(backend_id="default", remote_url="http://thanos-receive:19291/api/v1/receive")`
   - With filters: `write_relabel_configs=[{"source_labels": ["__name__"], "regex": "http_.*", "action": "keep"}]`
   - With custom queue: `queue_config={"capacity": 20000, "max_shards": 50}`

### Resources Used
| Resource | Purpose |
|---|---|
| `prom://tsdb/cardinality` | Quick cardinality overview |
| `prom://config/runtime` | Current scrape interval, retention, TSDB stats |
| `prom://best-practices` | Labeling and cardinality best practices |

---

## Workflow 6: Rule Management & Simulation

**When**: User needs to create, test, and deploy alerting/recording rules.

### Steps

1. **Draft rule from intent**
   - Call `prom_draft_alert_rule(intent="alert when 5xx errors exceed 5%")`
   - Returns PromQL expression + YAML rule definition

2. **Validate syntax**
   - Call `prom_check_rule_group(rules_yaml="...")`
   - Returns `{valid: true/false, error: "..."}`
   - Always validate before upsert to avoid wasting the HITL approval step

3. **Run synthetic tests**
   - Call `prom_run_rule_tests(rules_yaml="...", test_yaml="...")`
   - **Caveat**: Synthetic testing for `rate()` over mock counters is unreliable
     due to Prometheus rate extrapolation math. Prefer Step 4 for rate-based rules.

4. **Simulate against historical data**
   - Call `prom_simulate_firing_historical(backend_id="default", expr="...", for_duration="5m")`
   - Checks against real past data — much more reliable than synthetic tests

5. **Analyze firing history** (for existing rules)
   - Call `prom_analyze_firing_history(backend_id="default", alert_name="...", lookback="24h")`
   - Determines if existing alert is too noisy

6. **Tune alert thresholds** (for existing rules)
   - Call `prom_tune_alert_rule(backend_id="default", alert_name="...")`
   - Recommends better thresholds based on historical data

7. **Apply rule group** (MUTATES STATE)
   - Call `prom_upsert_rule_group(backend_id="default", group_name="api_errors", rules=[...])`
   - For K8s CRD storage: see Workflow 8

8. **Verify**
   - Read `prom://rules/groups` → confirm group appears
   - Query: `prom_query_instant(query="ALERTS{alertname='...'}", allow_raw_counters=true)`

### Advanced: Escalating Alerts (P1/P2)
Create two rules in a single group:
- **P2 Warning**: error rate > 5% for 1 minute
- **P1 Critical**: error rate > 5% for 3 minutes

The P2 enters `firing` state first. P1 stays `pending` until the 3-minute duration elapses.

### Prometheus Alert State Machine
1. **Inactive** → condition not met
2. **Pending** → condition met, `for` countdown running
3. **Firing** → `for` duration elapsed, alert routing to Alertmanager

---

## Workflow 7: Troubleshooting Failed Targets

**When**: A scrape target is down or metrics are missing.

### Steps

1. **Check failed targets**
   - Read `prom://topology/failed_targets`
   - Aggregated view of all down targets

2. **Check up status**
   - Call `prom_query_instant(backend_id="default", query="up{job='<job>'}")`
   - `up == 0` = target is down

3. **Check scrape duration**
   - Call `prom_query_instant(backend_id="default", query="scrape_duration_seconds{job='<job>'}")`
   - Duration > 10s = endpoint too slow

4. **Test endpoint directly** (bypasses Prometheus)
   - Call `prom_test_endpoint(endpoint_url="http://<service>.<ns>:<port>/metrics")`
   - Confirms whether the endpoint itself is healthy

5. **Check cardinality**
   - Read `prom://tsdb/cardinality`
   - High cardinality can cause scrape performance issues

### Diagnostic Matrix
| Symptom | Diagnostic | Root Cause | Fix |
|---|---|---|---|
| `up{job="x"} == 0` | Test endpoint directly | Connection refused, wrong port, pod not running | Verify Deployment, check port in ServiceMonitor |
| `scrape_duration > 10s` | Check cardinality | Endpoint too slow, too many metrics | Increase `scrape_timeout`, optimize endpoint |
| test_endpoint `ok: false` | Check errors array | Invalid format, auth required | Fix endpoint, add bearer token to ServiceMonitor |
| No `up{}` series at all | Check topology/services | ServiceMonitor not applied, wrong labels | Apply ServiceMonitor, check selector labels |
| 401 Unauthorized | Check endpoint auth | Endpoint requires authentication | Configure auth in ServiceMonitor |
| Context deadline exceeded | Check scrape_timeout | Timeout too short for endpoint | Increase timeout or optimize metrics endpoint |

### Resources for Troubleshooting
| Resource | Purpose |
|---|---|
| `prom://topology/failed_targets` | Quick triage of all down targets |
| `prom://topology/services` | Service catalog with health status |
| `prom://system/backends` | Backend connectivity check |
| `prom://config/runtime` | Verify scrape intervals and retention |

---

## Workflow 8: Autonomous K8s Rule CRD Upsert

**When**: Agent needs to discover and patch a PrometheusRule CRD without manual `kubectl`.

**Why this workflow exists**: `prom_upsert_rule_group` with `storage_mode: k8s_crd` requires
the exact Kubernetes `namespace` to patch the correct resource. `prom://rules/groups` fetches
from the Prometheus evaluation API and does NOT expose this metadata. This workflow bridges
the gap.

### Steps

1. **Inventory rule groups from Prometheus**
   - Read `prom://rules/groups`
   - Find the group name to modify (e.g., `"alertmanager.rules"`)

2. **Discover CRD metadata from Kubernetes**
   - Read `prom://kubernetes/prometheusrules`
   - Returns: `name`, `namespace`, `labels`, `groups` for every PrometheusRule CRD
   - Cross-reference group name from Step 1 to identify owning CRD
   - **Critical**: wrong namespace = silent duplicate CRD

3. **Understand the target rule**
   - Call `prom_describe_alert_rule(backend_id="default", group_name="<group>", alert_name="<alert>")`
   - Returns human-readable explanation of current expr, `for` duration, severity

4. **Compose updated rule**
   - Call `prom_draft_alert_rule(intent="...")` or compose YAML manually

5. **Validate syntax**
   - Call `prom_check_rule_group(rules_yaml="...")`
   - Confirm `valid: true` before proceeding

6. **Apply to cluster** (MUTATES STATE)
   - Call `prom_upsert_rule_group(backend_id="default", group_name="<group>", rules=[...], storage_mode="k8s_crd", namespace="<ns>")`
   - Uses the `namespace` discovered in Step 2

7. **Confirm change is live**
   - Read `prom://rules/groups` → confirm group present with expected config
   - Optional: `prom_query_instant(query="ALERTS{alertname='...'}", allow_raw_counters=true)`

### Prerequisites
| Requirement | How to Check |
|---|---|
| K8s integration enabled | `K8S_ENABLED=true` in server env |
| Prometheus Operator installed | `prom://kubernetes/prometheusrules` returns CRDs |
| kubeconfig accessible | `~/.kube/config` exists or `K8S_IN_CLUSTER=true` |

### Edge Cases
| Scenario | Behavior |
|---|---|
| K8s integration disabled | Resource returns error: `"Set K8S_ENABLED=true"` |
| No PrometheusRules in cluster | Resource returns `{prometheus_rules: [], total_crds: 0}` |
| Wrong namespace passed | Creates NEW CRD instead of patching — warn the user |
| Missing selector labels | CRD exists but Prometheus ignores it — check `ruleSelector` |

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `prom://rules/groups` | Step 1 | Active group names from Prometheus |
| `prom://kubernetes/prometheusrules` | Step 2 | CRD name, namespace, labels for upsert |
| `prom://config/runtime` | If needed | Verify `ruleSelector` labels |

---

## Workflow 9: Synthetic Endpoint Monitoring (Probes)

**When**: User requests uptime monitoring, synthetic monitoring, or wants to monitor a specific endpoint using native tools.

### Steps

1. **Deploy the Prober** (MUTATES CLUSTER)
   - Call `prom_install_exporter(exporter_type="blackbox_exporter", namespace="monitoring")`
   - Automatically injects a production-ready ConfigMap.

2. **Apply the Probe** (MUTATES CLUSTER)
   - Call `prom_apply_probe(targets=["https://talkops.ai"], prober_url="blackbox-exporter:9115", module="http_2xx")`
   - Automatically discovers `probeSelector` labels from the cluster. Do NOT use `kubectl`.

3. **Verify**
   - Call `prom_query_instant(query="probe_success")`
   - Confirm the endpoint is returning `1` (healthy).
