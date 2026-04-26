---
name: argo-rollouts-gitops
description: >-
  Use when the user asks to migrate deployments to rollouts, run canary or
  blue-green deployments, promote or abort a rollout, set up Prometheus analysis
  templates, monitor rollout health, or perform any Argo Rollouts operation.
  Also use when the user reports a rollout stuck, canary failing, or deployment
  not progressing — even if they don't mention Argo Rollouts by name. Triggers
  on keywords: Argo Rollouts, canary, blue-green, progressive delivery, rollout
  promote, rollout abort, rollout pause, migrate deployment, workloadRef,
  AnalysisTemplate, traffic weight, canary step, rollout degraded, rollout
  phase, experiment, A/B test, rollout stuck, canary failing, deployment not
  progressing.
metadata:
  author: talkops.ai
  version: '3.0'
  mcp_server: Argo Rollout MCP Server
compatibility: >-
  Requires Argo Rollout MCP Server. Kubernetes cluster with Argo Rollouts
  Controller installed and valid kubeconfig. Optional: Prometheus for
  AnalysisTemplate metrics, TraefikService for canary traffic routing.
---

# Argo Rollouts GitOps Skill

## When to Use

Load this skill ONLY for **state-modifying** Argo Rollouts operations: migrating Deployments
to Rollouts, creating rollouts, updating images, promoting/aborting lifecycle, configuring
AnalysisTemplates, creating experiments, or deleting rollouts.

Read-only queries (list rollouts, check status, view metrics, history) do NOT need this skill —
the sub-agent handles those directly via the Observability Fast-Path without loading any files.

## MCP Server Context

All tools are provided by the **Argo Rollout MCP Server** (server name: `argo_rollout_mcp_server`).

**Prerequisite:** A running Kubernetes cluster with Argo Rollouts Controller installed and a
valid `KUBECONFIG` mounted. If not installed, use the Helm MCP Server first.

**`apply` flag:** Every mutating generation tool accepts `apply=True/False`. `apply=False`
returns YAML for review without touching the cluster. Always default to `apply=False` on
first call for migrations and strategy changes, show the user the YAML, then re-call with
`apply=True` on confirmation.

## MCP Resources

Resource URIs are listed in the system prompt's Observability Fast-Path table — do not duplicate here.
For resource composition patterns during multi-step workflows, see `references/workflows.md`.

## Core Workflow: Explore → Plan → Implement → Verify

### 1. Explore
- If the task description provides rollout name, namespace, and action, skip to Planning.
- Otherwise: check `/memories/app-operator/operations-log.md` for recent operations context.
- Use `argorollout://rollouts/list` only if the target is completely unknown.
- For debugging: use `argorollout://health/{ns}/{name}/details` and
  `argorollout://metrics/{ns}/{svc}/summary`.

### 2. Plan — MANDATORY for all mutations

**Idempotency: NEVER create a resource without first checking if it already exists.**

| Before creating... | First check with... | If exists... |
|---|---|---|
| Rollout (migration) | `validate_deployment_ready` + `argorollout://rollouts/{ns}/{name}/detail` | Skip — rollout already active |
| Rollout (fresh) | `argorollout://rollouts/{ns}/{name}/detail` | Use `argo_update_rollout` instead |
| AnalysisTemplate | Check existing analysis config in rollout detail | Update — do not create duplicate |
| Experiment | `argorollout://experiments/{ns}/{name}/status` | Report status — do not create parallel run |

- Present a clear summary of intended changes.
- Call `request_human_input` with a formatted plan (see sub-agent prompt for templates).
- Wait for user approval before proceeding.

### 3. Implement
- If missing any required parameter, call `request_human_input`.
- For migrations: `apply=False` first → show YAML → `apply=True` after confirmation.
- For image updates: trigger via `argo_update_rollout`, then monitor step progression.
- If a rollout enters Degraded: use `argorollout://health/{ns}/{name}/details` for root cause.

### 4. Verify
- Subscribe to `argorollout://rollouts/{ns}/{name}/detail` — confirm phase=Healthy.
- For canary: check `argorollout://metrics/{ns}/{svc}/summary` at each pause step.
- For blue-green: maintain 5-minute post-cutover monitoring window.
- Do NOT declare success based solely on tool stdout.

## Tool Reference

### Migration & Generation (6 tools)

| Tool | Mutates Cluster | Purpose |
|------|----------------|---------|
| `validate_deployment_ready` | No | Pre-flight readiness check before any migration |
| `convert_deployment_to_rollout` | Yes (`apply` flag) | Convert existing Deployment → Rollout; auto-preserves probes/limits/env |
| `convert_rollout_to_deployment` | Yes (`apply` flag) | Reverse migration back to standard K8s Deployment |
| `argo_manage_legacy_deployment` | Yes | Scale/delete legacy Deployment **ONLY after workloadRef migration** (`convert_deployment_to_rollout`). NEVER use for image updates or routine rollout operations. |
| `create_stable_canary_services` | Yes (`apply` flag) | Generate stable+canary Services (prefer `convert_deployment_to_rollout` mode instead) |
| `generate_argocd_ignore_differences` | No | Generate ArgoCD `ignoreDifferences` config for Rollout integration |

**Migration mode choice:**
- `mode='direct'` — replaces the Deployment with a Rollout CRD (standalone clusters)
- `mode='workloadRef'` — Rollout references the existing Deployment (ArgoCD/Helm-managed apps; no pod duplication)

> **Note:** `argo_update_rollout` on workloadRef rollouts updates the backing Deployment — this is normal, NOT a trigger for `argo_manage_legacy_deployment`.

### Lifecycle Orchestration (4 tools)

| Tool | Action values | Purpose |
|------|--------------|---------| 
| `argo_create_rollout` | — | Create new Rollout from scratch (no existing Deployment) |
| `argo_update_rollout` | `image` \| `strategy` \| `traffic_routing` \| `workload_ref` | Update image, strategy config, traffic routing, or workloadRef |
| `argo_manage_rollout_lifecycle` | `promote` \| `promote_full` \| `pause` \| `resume` \| `abort` \| `skip_analysis` | All lifecycle state transitions in one tool |
| `argo_delete_rollout` | — | Remove Rollout from cluster |

### Validation, Traffic & Observation (3 tools)

| Tool | Mutates Cluster | Purpose |
|------|----------------|---------|
| `argo_configure_analysis_template` | Yes (`mode` flag) | Create+link Prometheus AnalysisTemplate (`mode=execute`) or generate YAML (`mode=generate_yaml`) |
| `argo_create_experiment` | Yes | Create ephemeral A/B experiment pods |
| `argo_delete_experiment` | Yes | Clean up experiment runs |

## Workflow Routing

Load `references/workflows.md` ONLY when executing a multi-step workflow from the table below.
Read ONLY the section matching the selected workflow — do NOT load the entire file.

| User Intent | Workflow Section |
|-------------|---------|
| Migrate existing Deployment → Rollout (standalone) | `#1-onboard--direct-migration` |
| Migrate ArgoCD/Helm-managed app → Rollout | `#2-onboard--workloadref-migration-argocdhelm` |
| Deploy new version with canary steps | `#3-canary-deployment` |
| Deploy new version with blue-green cutover | `#4-blue-green-deployment` |
| Standard rolling update | `#5-rolling-update` |
| Run A/B experiment | `#6-ab-testing-with-experiments` |
| Emergency rollback of broken canary/rollout | `#7-emergency-abort` |
| Revert Rollout → standard Deployment | `#8-reverse-migration--rollout-to-deployment` |
| Generate ArgoCD ignoreDifferences config | `#9-argocd-gitops-integration` |
| Zero-downtime migration for ArgoCD-managed apps | `#10-zero-downtime-migration-argocd` |

## Safety Rules — MUST Follow

1. **Always validate before migrating.** Run `validate_deployment_ready` before any
   `convert_deployment_to_rollout` call. Do not proceed if readiness score fails.

2. **apply=False first.** On migrations, strategy changes, and AnalysisTemplate creation:
   call with `apply=False`, show the generated YAML to the user, get confirmation, then
   call again with `apply=True`. Exception: image updates on an already-running rollout
   don't require this gate.

3. **Never direct-delete ArgoCD-managed Deployments.** In workloadRef mode, use
   `argo_manage_legacy_deployment(action='generate_scale_down_manifest')` → commit to Git →
   let ArgoCD apply it. Direct `delete_cluster` or `scale_cluster` bypasses GitOps and
   will cause drift.

4. **Abort before promote on degraded rollouts.** If detail shows phase=Degraded or error
   conditions, call `argo_manage_rollout_lifecycle(action='abort')` before attempting any
   promote. Promoting a degraded rollout propagates the broken version.

5. **skip_analysis only as last resort.** `action='skip_analysis'` bypasses Prometheus
   safety gates. Only invoke when explicitly instructed by the user with a stated reason.
   Always warn the user about the risk.

6. **Confirm destructive actions.** Always confirm before: `argo_delete_rollout`,
   `argo_delete_experiment`, `argo_manage_legacy_deployment(action='delete_cluster')`,
   `convert_rollout_to_deployment`. State what will be removed.

7. **Monitor after every lifecycle action.** After promote, abort, image update, or
   strategy change — subscribe to `argorollout://rollouts/{ns}/{name}/detail` and report
   phase transitions until stable (Healthy) or failed. For blue-green promotions: maintain
   a 5-minute post-cutover monitoring window. If error rate increases >20% relative to
   pre-promotion baseline (from `argorollout://metrics/{ns}/{svc}/summary`), recommend
   immediate abort.

8. **ArgoCD ignoreDifferences is required for workloadRef.** When ArgoCD manages the app,
   always run `generate_argocd_ignore_differences` after conversion and instruct the user
   to add it to their Application CR. Without it, ArgoCD will report OutOfSync on Rollout
   status fields.

9. **Autonomous promotion ceiling at 50%.** The agent may promote canary steps autonomously
   (based on healthy AnalysisRuns / metrics) up to 50% traffic weight. At or above 50%,
   pause and present a full metrics summary to the user. Only proceed to `promote_full`
   after explicit user approval.

10. **Handle Inconclusive AnalysisRuns explicitly.** If an AnalysisRun enters Inconclusive
    state: (a) Check health details for controller/provider errors. (b) Verify Prometheus
    connectivity via `argorollout://metrics/prometheus/status`. (c) If transient (provider
    timeout): retry by calling `argo_manage_rollout_lifecycle(action='resume')`. (d) If
    persistent: abort and report the failing metric provider to the user. (e) Never treat
    Inconclusive as passing — it is NOT a green signal.

## Gotchas

- **revisionHistoryLimit defaults to 10** — old ReplicaSets pile up in small clusters.
  Recommend setting to 3–5 for resource-constrained environments.
- **argocd-notifications fires on phase transitions** — an abort may trigger PagerDuty
  alerts. Warn users before abort if they have notifications configured.
- **workloadRef + HPA**: the HPA `scaleTargetRef` still points to the Deployment, NOT the
  Rollout. This is correct and intentional — do NOT change it to point at the Rollout.
- **Blue-green `autoPromotionEnabled: true`** silently promotes after `autoPromotionSeconds`
  elapses. If user expects manual cutover, this MUST be disabled explicitly.
- **AnalysisTemplate `count` vs `interval`**: setting `count: 1` means the metric is
  checked exactly once. For continuous monitoring, omit `count` and set `interval: 30s`.
- **Rollout `restartAt` annotation**: setting this causes an immediate rollout restart
  regardless of current phase. Never set it during an active canary — it will reset progress.

## Agentic Defaults — Apply Automatically

- **`strategy`**: `canary` when not specified (most common for progressive delivery).
- **`canary_steps`**: `5% → 10% → 25% → 50% → 100%` with pause gates when not specified.
- **`migration_mode`**: `direct` for standalone clusters; `workloadRef` if ArgoCD or Helm manages the app.
- **`namespace`**: derive from existing Deployment's namespace, or `default` if unknown.
- **`apply`**: always `False` on first call for migrations and strategy changes.
- **`strategy` (blue-green)**: auto-promotion disabled, manual cutover by default.

Always state derived defaults in the plan preview so the user can override.

## Response Format

- For rollout status, always report: phase + readyReplicas + current step + traffic weights.
- For canary progression, narrate step-by-step: "Step 2/5 — 25% canary traffic → [metric readings] → promoting."
- For health analysis, use: Status → Detected Issues → Root Cause → Immediate Actions → Preventive Measures.
- For generated YAML (`apply=False`), render in a code block with a clear "Review and confirm to apply" prompt.
- For AnalysisRun results, always report: metric name + measured value + threshold + pass/fail/inconclusive status.
- For post-promotion monitoring, report: time elapsed since cutover + error rate delta vs. baseline + any new crash events.
