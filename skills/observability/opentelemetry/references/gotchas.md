# OpenTelemetry Gotchas & Edge Cases

When managing OpenTelemetry on Kubernetes, keep these critical gotchas in mind. This document outlines common failure patterns and their diagnostic fixes.

---

## 1. RBAC for `k8sattributes` Processor

**The Issue**: The `k8sattributes` processor enriches telemetry with K8s metadata (namespace, pod name, node name) but requires `get`, `list`, `watch` permissions on K8s resources (Pods, ReplicaSets, Nodes, etc.). 
**Gotcha**: The OTel Operator **does not** create these RBAC resources automatically when you create an `OpenTelemetryCollector` CRD. Without them, the collector logs `pods is forbidden` errors and drops metadata.
**The Fix**: When using `otel_provision_collector(dry_run=False)`, the tool automatically creates the necessary `ClusterRole` and `ClusterRoleBinding` labeled `app.kubernetes.io/managed-by: talkops-mcp`. If deploying manually via `otel_patch_collector`, you must manually provision RBAC.

## 2. Processor Ordering is Critical

**The Issue**: Processors run sequentially. Wrong ordering can cause OOM kills or missing attributes.
**Gotcha**: 
- `memory_limiter` MUST be first to prevent OOM risk during load spikes.
- `batch` MUST be last so data is properly batched before export.
- `k8sattributes` must run BEFORE `batch`. If placed after, enrichment fails because data is already batched.
**The Fix**: Use `otel_validate_k8sattributes_order` to scan pipelines against the recommended order: `memory_limiter → k8sattributes → resourcedetection → resource → transform → filter → tail_sampling → batch`.

## 3. Tail Sampling Topology Constraints

**The Issue**: Tail sampling requires all spans for a given trace to arrive at the exact same collector instance to make a "keep/drop" decision based on full trace visibility (e.g. error-sampling, slow-traces).
**Gotcha**: If you run a collector as a DaemonSet or an un-sharded Deployment, spans for a trace might hit different collector pods, leading to fragmented traces and broken tail sampling.
**The Fix**: Use `StatefulSet` mode with trace-ID-aware routing, or a centralized Gateway Deployment layer dedicated to tail sampling. Use `otel_inspect_sampling_configuration` to ensure head/tail sampling don't conflict.

## 4. Filelog Self-Collection Feedback Loops

**The Issue**: The `filelog` receiver reads logs from `/var/log/pods`. 
**Gotcha**: The collector's own standard output logs are also stored in `/var/log/pods`. If the collector ingests its own logs, it creates an infinite feedback loop, exploding disk and CPU usage.
**The Fix**: Always use an exclude pattern. For example: `exclude_paths: ["/var/log/pods/*_otel-collector*/*/*.log"]`. Use `otel_check_filelog_safety` to audit missing exclusions and ensure `storage: file_storage` (checkpoints) is used to prevent data loss on pod restarts.

## 5. Language Detection Cascade & Manual Instrumentation

**The Issue**: `otel_list_instrumented_services` attempts to determine the language of a service.
**Gotcha**: Manually-instrumented apps (using SDKs instead of auto-instrumentation) do not have OTel Operator annotations. 
**The Fix**: The tool uses a 4-tier detection cascade: 
1. Annotations (`instrumentation.opentelemetry.io/inject-java`)
2. Image patterns (e.g., tags ending in `-python`)
3. Container names (e.g., `flask-api`)
4. Runtime env vars (e.g., `JAVA_HOME`, `PYTHONPATH`)
If an app fails detection, check its runtime environment variables.

## 6. Hardcoded `OTEL_*` Environment Variables

**The Issue**: Applying auto-instrumentation via annotations (`otel_annotate_deployment`).
**Gotcha**: The OTel Operator injects endpoints via `OTEL_EXPORTER_OTLP_ENDPOINT`. If the user's Deployment YAML already contains hardcoded `OTEL_*` environment variables, they will **silently override** the Operator-injected values, breaking data flow. 
**The Fix**: `otel_annotate_deployment(dry_run=True)` detects and warns about these conflicting env vars. Ensure the user removes them from their manifests.

## 7. SpanMetrics Cardinality Explosion

**The Issue**: SpanMetrics connector generates RED (Request, Error, Duration) metrics from traces.
**Gotcha**: Adding custom dimensions (like `http.target` or `user_id`) multiplies the cardinality. Exceeding 10 dimensions or using explicit histogram buckets with >20 bounds can severely degrade the Prometheus backend.
**The Fix**: Use `otel_detect_cardinality` and `otel_inspect_spanmetrics_config` to estimate series count. Generate remediation rules using `otel_gen_drop_attribute_rules` to drop high-cardinality attributes in a `transform` processor before they hit the connector.

## 8. eBPF Privileges

**The Issue**: eBPF auto-instrumentation (like Beyla) provides zero-code visibility.
**Gotcha**: eBPF agents often require elevated privileges. Some configurations erroneously request `SYS_ADMIN` or full Privileged mode, which is a massive security risk.
**The Fix**: Use `otel_analyze_ebpf_footprint` to scan for these risks. Recommend reducing capabilities to `BPF`, `PERFMON`, and `SYS_PTRACE`.

## 9. Loki Exporter Push Path

**The Issue**: The Loki exporter requires the explicit push API path `/loki/api/v1/push`.
**Gotcha**: When auto-discovering Loki services, the MCP server discovers `http://loki.loki:3100` as the endpoint. The OTel Loki exporter requires the full push path — sending to the root path produces `HTTP 404 "Not Found"`.
**The Fix**: The `otel_provision_collector` tool now **automatically appends** `/loki/api/v1/push` to Loki endpoints. This is idempotent — if the user provides the full path, it won't be duplicated. Similarly, Prometheus endpoints automatically get `/api/v1/otlp` appended.

## 10. Loki Multi-Tenant Authentication (`X-Scope-OrgID`)

**The Issue**: Loki running with `auth_enabled: true` (multi-tenant mode) requires an `X-Scope-OrgID` HTTP header on every push request.
**Gotcha**: After fixing the push path (Gotcha #9), Loki returns `HTTP 401 "Unauthorized": no org id` because the collector has no auth header configured.
**The Fix**: Use the `exporter_overrides` parameter on `otel_provision_collector` to inject the tenant header:
```python
otel_provision_collector(
    namespace="otel-demo",
    signals=["logs"],
    exporter_overrides={
        "loki": {"headers": {"X-Scope-OrgID": "my-tenant"}}
    },
    dry_run=True,
)
```
This is a generic mechanism — `exporter_overrides` works for any exporter type (Loki auth, Elasticsearch credentials, custom TLS, bearer tokens for OTLP gRPC, etc.).

## 11. OTel Operator Injects Telemetry Reader on Port 8888

**The Issue**: The OTel Operator automatically injects a `service.telemetry.metrics.readers` block into the collector's internal telemetry config. This configures a Prometheus pull reader that binds to `0.0.0.0:8888` inside the collector pod.

**Gotcha**: If the collector's `config.service.telemetry` also specifies a metrics address on port 8888 (or if the internal OTLP receiver/extensions happen to bind there), the pod will crash with `address already in use` and enter a `CrashLoopBackOff`.

**When this matters**: This is **only a problem when the collector service is exposed as `LoadBalancer` or uses `HostNetwork: true`**. With a standard `ClusterIP` service, the binding stays inside the pod and there is no conflict. If you see `CrashLoopBackOff` with bind errors immediately after provisioning:
1. Check `kubectl describe pod <pod> -n <namespace>` for `bind: address already in use` on port 8888.
2. If confirmed, change the telemetry metrics reader port in the collector spec:
   ```yaml
   spec:
     config:
       service:
         telemetry:
           metrics:
             readers:
               - pull:
                   exporter:
                     prometheus:
                       host: 0.0.0.0
                       port: 8890   # ← changed from 8888 to avoid conflict
   ```
3. Apply via `otel_patch_collector` with the updated config.

**Do NOT hardcode port 8890** in `otel_provision_collector` by default — this is only needed for non-ClusterIP collector services.

---

## 12. OTel Operator Appends `-collector` to CR Name for K8s Service

**The Issue**: When you create an `OpenTelemetryCollector` CR named `my-collector` in namespace `otel-demo`, the OTel Operator creates a Kubernetes Service named `my-collector-collector` (not `my-collector`).

**Gotcha**: This naming is not obvious and breaks ServiceMonitor wiring. If you pass `service_name="my-collector"` to `prom_apply_servicemonitor`, it auto-discovers the wrong selector (falls back to `{app: my-collector}` matching nothing), and Prometheus never scrapes the collector.

**The Fix**:
1. After provisioning, verify the exact service name: `kubectl get svc -n <namespace>`.
2. Use the full `-collector` suffixed name in `prom_apply_servicemonitor`:
   ```
   prom_apply_servicemonitor(
       service_name="my-collector-collector",  # not "my-collector"
       namespace="monitoring",
       target_namespace="otel-demo",
   )
   ```
3. When the collector namespace differs from the Prometheus Operator namespace, always use `target_namespace`.
