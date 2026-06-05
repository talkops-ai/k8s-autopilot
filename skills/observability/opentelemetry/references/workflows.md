# OpenTelemetry Operations — Workflow Playbook

Comprehensive step-by-step procedures for all 6 OpenTelemetry workflows.
Read this when executing state-modifying operations that require multi-step coordination.

---

## Workflow 1: Service Onboarding (Zero-to-Instrumented)

**When**: User has a custom application (Java, Python, Node.js, .NET, or Go) running on Kubernetes that needs OpenTelemetry auto-instrumentation.

### Steps

1. **Check language support**
   - Call `otel_lookup_instrumentation(language="<lang>")`
   - Returns `auto_instrumentation_available`, `annotation_key`, `sdk_package`, and frameworks.

2. **Check existing Instrumentation CR**
   - Read `otel://instrumentation/{namespace}/default`
   - Returns exporter endpoint, propagators, sampler config, per-language specs.

3. **Create Instrumentation CR (preview)**
   - Call `otel_patch_instrumentation(namespace="default", name="default", endpoint="http://otel-collector:4317", dry_run=True)`
   - Returns spec YAML for review.

4. **Apply Instrumentation CR** (MUTATES CLUSTER)
   - Call `otel_patch_instrumentation(..., dry_run=False)`
   - Creates the Instrumentation CRD.

5. **Annotate Deployment (preview)**
   - Call `otel_annotate_deployment(namespace="default", name="<app>", language="<lang>", dry_run=True)`
   - Returns annotation preview. Detects conflicting hardcoded `OTEL_*` env vars.

6. **Apply annotation** (MUTATES CLUSTER)
   - Call `otel_annotate_deployment(..., dry_run=False)`
   - Triggers rolling restart of the Deployment.

7. **Verify instrumentation**
   - Call `otel_list_instrumented_services(namespace="default")`
   - Confirms init container injected, OTEL_* env vars present, and detected language via 4-tier cascade.

### Resources Used
| Resource | When | Purpose |
|---|---|---|
| `otel://system/health` | Before Step 1 | Verify K8s connectivity and CRD availability |
| `otel://lang/{language}` | Step 1 | Detailed language capabilities |
| `otel://registry/languages` | Any time | Full language support catalog |
| `otel://instrumentation/{ns}/{name}` | Step 2 | Check if Instrumentation CR already exists |

---

## Workflow 2: Pipeline Investigation & Validation

**When**: User suspects pipeline misconfiguration — processors out of order, filelog self-collection, or enrichment gaps.

### Steps

1. **Get collector details**
   - Call `otel_get_collector(namespace="<ns>", name="<collector>", detail_level="full")`
   - Returns full pipeline topology, receivers, processors, exporters, raw YAML config.

2. **Validate processor ordering**
   - Call `otel_validate_k8sattributes_order(namespace="<ns>", name="<collector>")`
   - Checks all pipelines against recommended order (memory_limiter → k8sattributes → batch).

3. **Audit filelog safety**
   - Call `otel_check_filelog_safety(namespace="<ns>", name="<collector>")`
   - Detects missing checkpoint storage, self-collection loops, missing resource detection.

4. **Inspect enrichment profile**
   - Read `otel://k8s-enrichment/<ns>/<collector>`
   - Shows k8sattributes extracted metadata, labels, annotations, pod association.

5. **Check sampling config**
   - Call `otel_inspect_sampling_configuration(namespace="<ns>", collector_name="<collector>")`
   - Cross-references head + tail sampling; detects conflicts.

6. **Check SpanMetrics**
   - Read `otel://spanmetrics/<ns>/<collector>`
   - Shows dimensions, histogram config, pipeline wiring, cardinality estimates.

---

## Workflow 3: Metric Cardinality Audit & Remediation

**When**: SpanMetrics or other connectors are generating high-cardinality metrics.

### Steps

1. **Detect cardinality issues**
   - Call `otel_detect_cardinality(namespace="<ns>", name="<collector>")`
   - Returns issues, total estimated series, severity, and existing remediation status.

2. **Inspect SpanMetrics**
   - Call `otel_inspect_spanmetrics_config(namespace="<ns>", name="<collector>")`
   - Returns full profile: dimensions, histogram config, pipeline wiring.

3. **Generate remediation**
   - Call `otel_gen_drop_attribute_rules(attributes=["<attr1>", "<attr2>"], signal="metrics")`
   - Returns YAML snippet and instructions for transform processor.

4. **Apply Remediation** (Expert)
   - Call `otel_patch_collector(..., spec={...}, dry_run=True)` applying the generated transform processor.
   - Wait for user approval, then run with `dry_run=False`.

---

## Workflow 4: Sampling Strategy Review & Optimization

**When**: User needs to optimize trace sampling to reduce costs without losing important traces.

### Steps

1. **Inspect current sampling**
   - Call `otel_inspect_sampling_configuration(namespace="<ns>", collector_name="<collector>", instrumentation_cr_name="default")`
   - Cross-references head sampling from Instrumentation CR with tail sampling from collector.

2. **Get collector topology**
   - Call `otel_get_collector(namespace="<ns>", name="<collector>")`
   - Understand pipeline topology for sampling placement.

3. **Generate patch (head/tail/none)**
   - Call `otel_toggle_sampling_strategy(namespace="<ns>", collector_name="<collector>", target_mode="<mode>", dry_run=True)`
   - Example modes: `head`, `tail`, `none`. Generates CRD patch or collector config patch with default policies (error-sampling, slow-traces).

4. **Apply changes** (MUTATES CLUSTER)
   - Call `otel_toggle_sampling_strategy(..., dry_run=False)` after user approves.

---

## Workflow 5: Security Posture Audit

**When**: User needs to audit security posture of all OTel components (eBPF privileges, auto-instrumentation, RBAC).

### Steps

1. **Scan eBPF pods**
   - Call `otel_analyze_ebpf_footprint(namespace="<ns>")`
   - Returns risk level, privileged containers, SYS_ADMIN capabilities, and hostPID.

2. **List instrumented services**
   - Call `otel_list_instrumented_services(namespace="<ns>")`
   - Reviews init containers and annotations for injection safety.

3. **List collectors and Inspect RBAC**
   - Call `otel_list_collectors(namespace="<ns>")`
   - Read `otel://k8s-enrichment/<ns>/<collector>` for each collector to check RBAC requirements (k8sattributes).

4. **Inspect Target Allocator**
   - Call `otel_inspect_target_allocator_state(namespace="<ns>", name="<collector>")`
   - Checks ServiceMonitor/PodMonitor selectors and prometheusCR enablement.

---

## Workflow 6: Intent-Driven Collector Provisioning (Smart)

**When**: User needs a collector but doesn't want to write CRD YAML. Auto-discovers backends and generates best-practice config.

### Steps

1. **Provision collector (preview)**
   - Call `otel_provision_collector(namespace="<ns>", signals=["traces", "metrics"], dry_run=True)`
   - Returns generated config, auto-discovered endpoints, mode rationale, resource sizing, and recommendations.
   - Example additional parameters: `enable_spanmetrics=True`, `enable_filelog=True`.

2. **Review auto-discovered targets**
   - Verify the discovered backends are correct in the dry_run output.

3. **Apply collector** (MUTATES CLUSTER)
   - Call `otel_provision_collector(..., dry_run=False)`
   - Automatically creates `OpenTelemetryCollector` CRD, plus `ClusterRole` and `ClusterRoleBinding` for the `k8sattributes` processor.

4. **Verify deployment**
   - Call `otel_get_collector(namespace="<ns>", name="<name>")`
   - Confirm collector is running and config is correct.
