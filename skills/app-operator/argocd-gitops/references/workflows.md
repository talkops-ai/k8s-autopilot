# ArgoCD Workflows Reference

Detailed step sequences for guided operations. Load this file when executing any multi-step workflow.

## Table of Contents
1. [Repository Onboarding](#1-repository-onboarding)
2. [Full Application Deployment](#2-full-application-deployment)
3. [Deploy New Version](#3-deploy-new-version)
4. [Debug Application Issues](#4-debug-application-issues)
5. [Rollback Decision](#5-rollback-decision)
6. [Post-Deployment Validation](#6-post-deployment-validation)
7. [Setup ArgoCD Project](#7-setup-argocd-project)
8. [Lifecycle & Maintenance](#8-lifecycle--maintenance)
9. [Declarative GitOps (Manifest Generation)](#9-declarative-gitops-manifest-generation)
10. [Update Existing Project](#10-update-existing-project)

---

## 1. Repository Onboarding

**Trigger phrases:** "onboard repo", "add repository", "register GitHub repo"

```
Step 1 → Check env: confirm GIT_PASSWORD (HTTPS) or SSH_PRIVATE_KEY_PATH (SSH) is set
Step 2 → validate_repository_connection   — test connectivity before registering
Step 3 → onboard_repository_https         — HTTPS auth
      OR onboard_repository_ssh            — SSH auth
Step 4 → get_repository                   — confirm successful registration
```

**Decision:** Ask user which auth method if not stated. Default to HTTPS if `GIT_PASSWORD` is available.

---

## 2. Full Application Deployment

**Trigger phrases:** "deploy my application", "deploy from GitHub", "deploy from scratch", "end-to-end deployment"

**Time estimate:** ~1–2 minutes

```
Step 1 → list_repositories               — check if repo already registered
       [If not registered] → Run Workflow 1 (Repository Onboarding) first
Step 2 → validate_application_config      — validate before creating
Step 3 → create_application               — create the ArgoCD app
Step 4 → get_application_diff             — REQUIRED: show preview of resources to be created
       → Ask user to confirm before proceeding
Step 5 → sync_application (dry_run=true)  — dry-run on production clusters
       → Show dry-run output; confirm to proceed
Step 6 → sync_application                 — execute deployment
Step 7 → get_sync_status (poll)           — monitor until Synced or Failed
Step 8 → get_application_details          — validate: health=Healthy, sync=Synced
       [On failure] → cancel_deployment   — abort if sync is stuck
       [On failure] → Run Workflow 4 (Debug)
```

---

## 3. Deploy New Version

**Trigger phrases:** "deploy version X.Y.Z", "deploy new version", "push new release"

```
Step 1 → get_application_details          — capture current state and revision
Step 2 → validate_application_config      — validate new config (if config changed)
Step 3 → get_application_diff             — diff current vs. target revision
       → Present diff; ask for confirmation
Step 4 → sync_application (revision=X.Y.Z)— sync to target revision
Step 5 → get_sync_status (poll)           — monitor progress
Step 6 → get_application_logs             — check for errors post-deployment
Step 7 → Run Workflow 6 (Post-Deployment Validation)
```

---

## 4. Debug Application Issues

**Trigger phrases:** "app not working", "application degraded", "pods crashing", "debug", "troubleshoot"

**Time estimate:** ~15 seconds (fully automated)

```
Step 1 → get_application_details          — status, pod count, health state
Step 2 → get_application_logs             — automatic error detection (scan for errors)
Step 3 → get_application_events           — Kubernetes events (CrashLoopBackOff, OOMKilled, etc.)
Step 4 → Analyze: correlate logs + events → identify root cause
Step 5 → Report using this structure:
          🔍 Application Status: [HEALTHY/DEGRADED/PROGRESSING]
          📋 Errors Detected: [list with counts and timestamps]
          🎯 Root Cause: [concise diagnosis]
          💡 Immediate Fixes: [numbered action list]
          🛡️ Preventive Measures: [numbered list]
```

**Common root causes to check:**
- CrashLoopBackOff → check env vars, secrets, resource limits
- ImagePullBackOff → registry auth, image tag exists
- Pending pods → node resources, PVC binding, tolerations
- DB connection errors → service DNS, network policy, secret values
- OutOfSync → manual drift, missing resources, finalizers blocking delete

---

## 5. Rollback Decision

**Trigger phrases:** "rollback", "revert deployment", "undo last deploy", "deployment broken"

**Time estimate:** ~1 minute

```
Step 1 → get_application_details          — get deployment history + current revision
Step 2 → get_application_diff             — preview what changes rollback will make
       → Present history; ask user to confirm target revision
Step 3 → CRITICAL: Check auto-sync status from Step 1 details
       [If auto-sync enabled] → update_application → disable auto-sync FIRST
       Reason: ArgoCD will immediately re-sync forward if auto-sync remains on
Step 4 → rollback_application             — rollback to previous revision
      OR rollback_to_revision (id=N)      — rollback to specific revision
Step 5 → get_sync_status (poll)           — monitor rollback progress
Step 6 → get_application_logs             — validate recovery, no new errors
Step 7 → Report: old revision → new revision, health status confirmed
```

---

## 6. Post-Deployment Validation

**Trigger phrases:** "validate deployment", "check deployment health", "post-deploy check"

```
Step 1 → get_sync_status                  — confirm sync=Synced (not OutOfSync)
Step 2 → validate_application_config      — config integrity check
Step 3 → get_application_logs             — scan for errors in recent logs
Step 4 → get_application_details          — confirm health=Healthy, correct replica count
       [Subscribe] argocd://application-metrics/{cluster}/{app} — live pod/replica metrics
```

**Pass criteria:** `sync=Synced`, `health=Healthy`, no errors in logs within last 60s.

---

## 7. Setup ArgoCD Project

**Trigger phrases:** "create project for team", "multi-tenancy", "set up ArgoCD project", "namespace isolation"

```
Step 1 → Gather requirements:
          - Project name
          - Allowed source repositories (patterns, wildcards OK)
          - Destination clusters and namespaces
          - Cluster resource whitelist/blacklist
Step 2 → create_project                   — create with RBAC policies
Step 3 → get_project                      — verify project created correctly
Step 4 → generate_project_manifest        — output AppProject YAML for GitOps tracking
       → Recommend committing manifest to Git for declarative management
```

---

## 8. Lifecycle & Maintenance

**Quick operations — no full workflow needed.**

| Operation | Tool Sequence |
|-----------|--------------|
| Enable auto-sync + self-healing | `update_application` (set syncPolicy) |
| Force refresh (stuck app) | `soft_refresh` → if still stuck → `hard_refresh` |
| Remove orphaned resources | `prune_resources` → confirm with user first |
| Delete app + resources | `delete_application` (confirm cascade=true intent) |
| Cancel stuck deployment | `cancel_deployment` → `get_sync_status` to confirm cancelled |
| List all app health | `list_applications` → summarize healthy/degraded/progressing counts |

---

## 9. Declarative GitOps (Manifest Generation)

**Trigger phrases:** "generate manifest", "YAML for GitOps", "AppProject YAML", "repository secret"

```
generate_repository_secret_manifest → produces v1/Secret with ArgoCD labels
generate_project_manifest           → produces AppProject CRD YAML
```

**Always recommend:** Commit generated YAML to the GitOps repository for declarative management. Do not leave configuration only in the ArgoCD UI state.

---

## 10. Resource Usage Patterns

These patterns dictate how to orchestrate MCP resource subscriptions for monitoring scenarios:

### Active Deployment Monitoring
```text
1. Subscribe: argocd://sync-operations/{cluster}     — track sync progress (2s updates)
2. Subscribe: argocd://deployment-events/{cluster}   — capture events in real-time
3. On completion: argocd://application-metrics/{cluster}/{app} — verify pod health
```

### Cluster Health Dashboard
```text
1. Subscribe: argocd://cluster-health/{cluster}      — overall counts (30s refresh)
2. Subscribe: argocd://applications/{cluster}        — per-app status (5s refresh)
3. On degraded apps: run Debug Application Issues workflow
```

### Post-Incident Review
```text
1. get_application_events → Kubernetes event timeline
2. argocd://deployment-events/{cluster} → deployment event history
3. get_application_logs → correlate with error timeline
```

---

## 10. Update Existing Project

**Trigger phrases:** "add repo to project", "tie repo to project", "update project limits", "add namespace to project"

```
Step 1 → get_project                      — IMPORTANT: Fetch existing project state first
Step 2 → Extract current arrays           — Save existing source_repos, destinations, etc.
Step 3 → Append new items                 — e.g., add new repo URL to the source_repos list
Step 4 → create_project                   — Call create_project with the UPDATED list. This tool performs an UPSERT.
Step 5 → get_project                      — Verify the update succeeded
```
