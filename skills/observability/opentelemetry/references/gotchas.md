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
