# Argo Rollouts Workflows Reference

Detailed step sequences for all progressive delivery operations. Load this file when executing any multi-step workflow.

## Table of Contents
1. [Onboard — Direct Migration](#1-onboard--direct-migration)
2. [Onboard — workloadRef Migration (ArgoCD/Helm)](#2-onboard--workloadref-migration-argocdhelm)
3. [Canary Deployment](#3-canary-deployment)
4. [Blue-Green Deployment](#4-blue-green-deployment)
5. [Rolling Update](#5-rolling-update)
6. [A/B Testing with Experiments](#6-ab-testing-with-experiments)
7. [Emergency Abort](#7-emergency-abort)
8. [Reverse Migration — Rollout to Deployment](#8-reverse-migration--rollout-to-deployment)
9. [ArgoCD GitOps Integration](#9-argocd-gitops-integration)
10. [Zero-Downtime Migration (ArgoCD)](#10-zero-downtime-migration-argocd)

---

## 1. Onboard — Direct Migration

**Trigger phrases:** "migrate deployment to rollout", "convert to Argo Rollout", "onboard to progressive delivery"
**Use when:** App is managed directly via kubectl or Helm without ArgoCD GitOps sync.
**Guided prompt:** `onboard_application_guided`

```
Step 1 → ArgoRollout:validate_deployment_ready
         • Pass: namespace + deployment name
         • Must pass all checks before proceeding
         • On failure: report failing checks; do not convert

Step 2 → ArgoRollout:convert_deployment_to_rollout (apply=False, mode='direct')
         • Auto-fetches Deployment from cluster
         • Preserves: resource limits, probes, env vars, replicas
         • Auto-discovers existing Service port and selector
         • Returns: Rollout CRD YAML + stable/canary Service YAMLs
         → Show YAML to user; confirm before applying

Step 3 → ArgoRollout:convert_deployment_to_rollout (apply=True, mode='direct')
         • Creates: Rollout CRD + stable Service + canary Service

Step 4 [Optional] → ArgoRollout:argo_configure_analysis_template (mode='execute')
         • Link Prometheus AnalysisTemplate for automated health checks
         • Parameters: prometheus_url, error_rate_threshold, latency_p99_threshold

Step 5 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         • Confirm phase=Healthy, readyReplicas matches desired
```

**Decision — strategy choice (ask user if not stated):**
- `strategy='canary'` → traffic splits, step-based promotion (recommended for stateless services)
- `strategy='bluegreen'` → full preview environment, instant cutover (recommended for stateful or regulated services)
- `strategy='rolling'` → standard max-surge rolling (simplest; no traffic splitting)

---

## 2. Onboard — workloadRef Migration (ArgoCD/Helm)

**Trigger phrases:** "migrate ArgoCD-managed app", "workloadRef migration", "no pod duplication migration", "Helm-managed rollout"
**Use when:** App is managed by ArgoCD or Helm. Direct replacement would cause GitOps drift.
**Guided prompt:** `onboard_application_guided`

```
Step 1 → ArgoRollout:validate_deployment_ready
         • Confirm deployment readiness

Step 2 → ArgoRollout:convert_deployment_to_rollout (apply=False, mode='workloadRef')
         • Rollout references existing Deployment via workloadRef
         • No pod duplication — Rollout takes over scheduling from Deployment
         • Returns: Rollout CRD YAML only (Deployment stays)
         → Show YAML; confirm

Step 3 → ArgoRollout:convert_deployment_to_rollout (apply=True, mode='workloadRef')
         • Applies Rollout CRD to cluster

Step 4 → ArgoRollout:argo_manage_legacy_deployment (action='generate_scale_down_manifest')
         • CRITICAL: generates a patch to scale Deployment replicas to 0
         • Do NOT use action='scale_cluster' or action='delete_cluster' — ArgoCD will overwrite it
         → Instruct user to commit this manifest to their Git repo
         → ArgoCD applies it on next sync (zero-downtime, fully declarative)

Step 5 → ArgoRollout:generate_argocd_ignore_differences
         • REQUIRED: generates ignoreDifferences block for the Application CR
         • Include: Rollout status fields, AnalysisRun fields, Deployment replicas
         → Instruct user to add to their ArgoCD Application manifest

Step 6 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         • Confirm Rollout is healthy and managing pods
```

**Why workloadRef:** The Rollout CRD references the existing Deployment spec. No duplicate pods. ArgoCD continues owning the Deployment; Rollout takes over scheduling. `ignoreDifferences` prevents false OutOfSync alerts on Rollout-controlled fields.

---

## 3. Canary Deployment

**Trigger phrases:** "canary deploy", "deploy with canary", "progressive traffic shift", "canary steps"
**Guided prompt:** `canary_deployment_guided`

```
Pre-flight:
→ Subscribe: argorollout://rollouts/{ns}/{name}/detail   — confirm current phase=Healthy

Step 1 → ArgoRollout:argo_update_rollout (update_type='image', image='app:v2')
         • Triggers new canary ReplicaSet; begins step 1 of canary steps

Step 2 → Subscribe: argorollout://rollouts/{ns}/{name}/detail (poll)
         • Watch: phase, currentStepIndex, traffic weights
         • Report each step transition to user

At each pause step (human gate):
→ Read: argorollout://metrics/{ns}/{svc}/summary
  — Check: error rate, P99 latency, request rate vs. baseline
→ If metrics healthy:
    ArgoRollout:argo_manage_rollout_lifecycle (action='promote')   — advance to next step
→ If metrics degraded:
    ArgoRollout:argo_manage_rollout_lifecycle (action='abort')     — roll back to stable immediately

Final step → promote_full:
→ ArgoRollout:argo_manage_rollout_lifecycle (action='promote_full')
  — Skips all remaining steps; shifts 100% traffic to canary
→ Subscribe: argorollout://rollouts/{ns}/{name}/detail
  — Confirm phase=Healthy, all replicas on new image

Abort at any time:
→ ArgoRollout:argo_manage_rollout_lifecycle (action='abort')
  — Instantly returns all traffic to stable ReplicaSet
```

**Standard canary weight progression:** 5% → 10% → 25% → 50% → 100% (with pause gates between each)

**With Prometheus analysis (automated):** Configure `argo_configure_analysis_template` before triggering — canary auto-aborts if error rate or latency thresholds breach during any step.

---

## 4. Blue-Green Deployment

**Trigger phrases:** "blue-green deploy", "preview environment", "zero-downtime cutover", "instant rollback"
**Guided prompt:** `blue_green_deployment_guided`

```
Step 1 → ArgoRollout:argo_update_rollout (update_type='image', image='app:v2')
         • Creates preview (green) ReplicaSet alongside active (blue)
         • No traffic to green yet — preview only

Step 2 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         • Watch: previewReplicaSet health, readyReplicas
         • Report when green is fully up

Step 3 [Optional] → Smoke test / validation on preview service
         • User can validate on preview endpoint before cutover

Step 4 → argorollout://metrics/{ns}/{svc}/summary   — baseline check on blue
         → Confirm stable metrics before switching

Step 5 → ArgoRollout:argo_manage_rollout_lifecycle (action='promote')
         • Cuts over traffic: green becomes active (blue)
         • Old blue (now stable) retained for instant rollback window

Step 6 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         • Confirm phase=Healthy on new active
         → ArgoRollout:argo_manage_rollout_lifecycle (action='abort') if issues detected
           — Instantly flips traffic back to old stable
```

**Blue-Green advantage:** Full parallel environments — zero request drops on cutover. Old stable retained until explicitly scaled down.

---

## 5. Rolling Update

**Trigger phrases:** "rolling update", "deploy new version", "rolling deploy"
**Guided prompt:** `rolling_update_guided`

```
Step 1 → ArgoRollout:validate_deployment_ready (if first rollout)
         — skip if rollout already exists

Step 2 → argorollout://rollouts/{ns}/{name}/detail   — confirm phase=Healthy before updating

Step 3 → ArgoRollout:argo_update_rollout (update_type='image', image='app:v2')
         • Triggers rolling update with configured maxSurge/maxUnavailable

Step 4 → Subscribe: argorollout://rollouts/{ns}/{name}/detail (poll)
         • Report: readyReplicas progression, phase transitions

Step 5 → argorollout://health/{ns}/{name}/details   — on any degraded signal
         • Identify crash cause
         → ArgoRollout:argo_manage_rollout_lifecycle (action='abort') if degraded
```

---

## 6. A/B Testing with Experiments

**Trigger phrases:** "A/B test", "experiment", "baseline vs candidate", "split test"

```
Step 1 → ArgoRollout:argo_create_experiment
         • Parameters: name, namespace, duration (e.g., 30m)
         • Defines: baseline template (stable spec) + candidate template (canary spec)
         • Creates ephemeral side-by-side pods — no traffic impact on main rollout

Step 2 → Subscribe: argorollout://experiments/{ns}/{name}/status (poll)
         • Monitor: analysis metrics, progression, phase

Step 3 → argorollout://metrics/{ns}/{svc}/summary
         • Compare baseline vs. candidate on: error rate, P99 latency, request rate

Step 4a [Candidate wins] → ArgoRollout:argo_delete_experiment
         → ArgoRollout:argo_update_rollout (update_type='image', image='candidate-image')
         — Proceed with canary or blue-green rollout of winner

Step 4b [Baseline wins] → ArgoRollout:argo_delete_experiment
         — Keep current stable; discard candidate

Step 4c [Inconclusive] → Extend experiment duration or adjust traffic split
```

---

## 7. Emergency Abort

**Trigger phrases:** "abort rollout", "rollback canary", "something is wrong", "emergency rollback", "revert to stable"
**Time estimate:** ~5 seconds

```
Step 1 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         — Capture: current phase, step, canary weight, images in use

Step 2 → argorollout://health/{ns}/{name}/details
         — Identify: crash status, error conditions

Step 3 → ArgoRollout:argo_manage_rollout_lifecycle (action='abort')
         • IMMEDIATE: all traffic returns to stable ReplicaSet
         • Canary pods scaled down; stable becomes 100% active
         • Does NOT delete the new image — preserves it for retry

Step 4 → Subscribe: argorollout://rollouts/{ns}/{name}/detail (poll)
         • Confirm phase returns to Degraded → Healthy (after stable recovers)

Step 5 → argorollout://health/{ns}/{name}/details + argorollout://metrics/{ns}/{svc}/summary
         • Report: what caused the abort, current stable version, error timeline

Step 6 [Recovery] → ArgoRollout:argo_manage_rollout_lifecycle (action='resume')
         — ONLY after root cause is identified and fixed in the new image
         — Re-triggers canary with new build
```

**abort vs. promote_full distinction:**
- `abort` → instant traffic return to stable, preserves rollout state for retry
- `promote_full` → skips analysis gates, commits 100% to canary (use only when verified healthy)

---

## 8. Reverse Migration — Rollout to Deployment

**Trigger phrases:** "convert rollout back to deployment", "abandon Argo Rollouts", "revert to standard deployment"

```
Step 1 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         — Capture current image, replicas, strategy config

Step 2 → ArgoRollout:convert_rollout_to_deployment (apply=False)
         — Returns standard Deployment YAML with RollingUpdate strategy
         → Show YAML; confirm rollback strategy parameters (maxSurge, maxUnavailable)
         → Confirm user understands: canary/blue-green features will be lost

Step 3 → ArgoRollout:convert_rollout_to_deployment (apply=True)
         — Applies standard Deployment; removes Rollout CRD

Step 4 → Verify: kubectl get deployment {name} -n {ns}
         — Confirm Deployment exists and pods are healthy
```

---

## 9. ArgoCD GitOps Integration

**Trigger phrases:** "ArgoCD ignoreDifferences", "ArgoCD showing OutOfSync", "ArgoCD integration", "Rollout with ArgoCD"

```
Step 1 → ArgoRollout:generate_argocd_ignore_differences
         • Parameters: app_name, namespace
         • Optional: include_deployment_replicas=True (for workloadRef)
         • Optional: include_traefik_service=True (if using Traefik traffic routing)
         → Returns: ignoreDifferences YAML block for Application CR

Step 2 → Instruct user to add this block to their ArgoCD Application manifest:
         spec:
           ignoreDifferences:
             [generated block here]

Step 3 → Instruct user to sync their Application in ArgoCD
         — OutOfSync alert on Rollout status fields will be suppressed

Common ignoreDifferences fields covered:
- Rollout .status fields (phase, canaryStatus, stableRS)
- AnalysisRun ownership references
- Deployment .spec.replicas (workloadRef mode — Rollout controls replica count)
- TraefikService weight fields (if traffic routing enabled)
```

---

## 10. Zero-Downtime Migration (ArgoCD)

**Trigger phrases:** "zero-downtime migration with ArgoCD", "migrate without downtime", "ArgoCD managed zero downtime"

This is a precise combination of Workflow 2 (workloadRef) with coordinated ArgoCD sync gates.

```
Step 1 → ArgoRollout:validate_deployment_ready
Step 2 → ArgoRollout:convert_deployment_to_rollout (apply=False, mode='workloadRef')
         → Review YAML — confirm workloadRef points to correct Deployment
Step 3 → ArgoRollout:convert_deployment_to_rollout (apply=True, mode='workloadRef')
         — Rollout CRD applied; pods continue running under Deployment
Step 4 → ArgoRollout:generate_argocd_ignore_differences (include_deployment_replicas=True)
         → User commits ignoreDifferences to Application CR in Git
Step 5 → User triggers ArgoCD sync
         — ArgoCD detects Rollout; suppresses OutOfSync on controlled fields
Step 6 → ArgoRollout:argo_manage_legacy_deployment (action='generate_scale_down_manifest')
         → User commits scale-down patch (replicas=0) to Git
Step 7 → User triggers ArgoCD sync again
         — ArgoCD scales Deployment to 0; Rollout takes full ownership
         — Zero downtime: pods were already managed by Rollout since Step 3
Step 8 → Subscribe: argorollout://rollouts/{ns}/{name}/detail
         — Confirm: phase=Healthy, Rollout owns all pods
```
