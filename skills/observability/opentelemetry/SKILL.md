---
name: opentelemetry
description: >-
  Manages OpenTelemetry pipelines and instrumentation via MCP tools. Use when the user asks to
  provision collectors, onboard applications to OTel (auto-instrumentation), validate or investigate
  pipeline processors, audit metric cardinality (SpanMetrics/Histograms), optimize sampling strategies
  (head vs. tail), or assess eBPF security posture. Triggers on keywords: OpenTelemetry, OTel,
  collector, traces, spanmetrics, auto-instrumentation, tail sampling.
  Do NOT use for Prometheus-only metrics or Alertmanager routing unless directly related to an OTel pipeline.
metadata:
  author: talkops.ai
  version: '1.0'
  mcp_server: OpenTelemetry MCP Server
compatibility: >-
  Requires OpenTelemetry MCP Server (server name: opentelemetry-mcp-server).
  Provides discovery, collector, instrumentation, validation, governance, sampling, and spanmetrics tools.
---

# OpenTelemetry Operations Skill

## When to Use

Load this skill ONLY for **state-modifying** OpenTelemetry operations: provisioning a collector,
creating or updating an Instrumentation CRD, annotating a deployment for auto-instrumentation,
toggling sampling strategy, or patching collector configurations.

Read-only queries (listing collectors, investigating pipelines, auditing cardinality)
are handled directly by the sub-agent via the Query Fast-Path and do not require loading this SKILL file.

## Core Workflow: Explore → Plan → Implement → Verify

### 1. Explore
- If the task description provides all required parameters, skip to Planning.
- Check `/memories/observability/operations-log.md` for recent operations context.
- Use `otel_list_collectors` to discover existing collectors.
- Use `otel_lookup_instrumentation` to understand language capabilities.
- Use `otel_inspect_sampling_configuration` to understand existing head/tail rules before modifying sampling.

### 2. Plan — MANDATORY for all mutations
- State-modifying tools (`otel_provision_collector`, `otel_patch_collector`, `otel_patch_instrumentation`, `otel_annotate_deployment`, `otel_toggle_sampling_strategy`, `otel_enable_spanmetrics_for_service`, `otel_gen_drop_attribute_rules`) support a `dry_run` parameter.
- ALWAYS call the tool with `dry_run=True` first to generate the preview configuration.
- Present a clear summary of intended changes (with warnings/recommendations from the tool) to the user.
- Call `request_human_input` with a formatted plan. Wait for approval.

### 3. Implement
- Once approved, call the same tool with `dry_run=False` (using the EXACT same parameters) to apply the change.
- Note that `otel_annotate_deployment` triggers a rolling restart of the target deployment. Ensure the user is aware of this during planning.

### 4. Verify & Diagnose (MANDATORY)
**Never declare success based solely on tool stdout.** Always run a verification query.

- After provisioning or patching a collector: Use `otel_get_collector` to verify it deployed successfully.
- After patching instrumentation / annotating deployment: Use `otel_list_instrumented_services` to verify the auto-instrumentation status, annotation presence, and init container injection.

#### Failure Diagnosis Protocol
If verification fails, use diagnostic resources:
- `otel://system/health`: to ensure basic connectivity and OTel CRD presence.
- Check collector pipelines using `otel_validate_k8sattributes_order` and `otel_check_filelog_safety`.

**Out-of-Scope Escalation**: You MUST exhaust all relevant MCP tools and resources first. If the root cause remains hidden, explicitly return: "I have exhausted my MCP diagnostic tools. Further diagnosis requires cluster access. Please check pod logs via `kubectl logs`."

## Workflow Reference

For detailed step-by-step procedures with expected outputs, edge cases, and parameter
patterns, read [references/workflows.md](references/workflows.md).

| User Intent | Workflow | Key Entry Point |
|---|---|---|
| Service Onboarding | Zero-to-Instrumented | `otel_lookup_instrumentation` → `otel_patch_instrumentation` → `otel_annotate_deployment` |
| Pipeline Investigation | Verify processor ordering / safety | `otel_get_collector` → `otel_validate_k8sattributes_order` → `otel_check_filelog_safety` |
| Cardinality Audit | Detect & remediate high cardinality | `otel_detect_cardinality` → `otel_inspect_spanmetrics_config` → `otel_gen_drop_attribute_rules` |
| Sampling Review | Optimize trace sampling | `otel_inspect_sampling_configuration` → `otel_toggle_sampling_strategy` |
| Security Audit | Scan eBPF and collector RBAC | `otel_analyze_ebpf_footprint` |
| Collector Provisioning | Intent-driven deployment | `otel_provision_collector` |

## Safety Rules — MUST Follow

1. **Two-Layer HITL Model.** State-modifying operations have TWO safety layers:
   - **Layer 1 — Planning Gate**: Call tool with `dry_run=True`, then use `request_human_input` to present the plan.
   - **Layer 2 — Middleware Gate**: `HumanInTheLoopMiddleware` auto-pauses on the gated tools when `dry_run=False`.

2. **Always Use Dry Run.** Never set `dry_run=False` unless the user has explicitly approved the plan.

3. **Missing Inputs Protection.** Never hallucinate namespace or service names.

## Gotchas

For common failure patterns, edge cases, and diagnostic fixes, read
[references/gotchas.md](references/gotchas.md).

## Agentic Defaults — Apply Automatically

- **`namespace`**: Ensure you have a target namespace; do not assume "default" without confirming with the user if ambiguous.
- **`dry_run`**: Always defaults to `True` for modifying tools.
