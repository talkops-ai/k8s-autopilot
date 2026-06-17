---
name: argocd-gitops
description: >-
  Manages ArgoCD GitOps workflows via MCP tools. Use when the user asks to
  deploy, sync, rollback, debug, onboard repositories, manage projects, monitor
  applications, or perform any ArgoCD operation. Also use when the user reports
  an app as degraded, out-of-sync, or failing — even if they don't mention
  ArgoCD by name. Triggers on keywords: ArgoCD, GitOps, sync, rollback, deploy
  application, Argo application, application health, out-of-sync, app degraded,
  onboard repo, ArgoCD project, deployment failed, create application, delete.
metadata:
  author: talkops.ai
  version: '2.0'
  mcp_server: ArgoCD MCP Server
compatibility: >-
  Requires ArgoCD MCP Server (server name: argocd_mcp_server). Provides 29 ArgoCD tools.
---

# ArgoCD GitOps Skill

## When to Use

Load this skill ONLY for **state-modifying** ArgoCD operations: creating apps, syncing,
rolling back, deleting, onboarding repos, or managing projects.

Read-only queries (list apps, get status, view logs) do NOT need this skill — the sub-agent
handles those directly via the Query Fast-Path without loading any skill files.

## Core Workflow: Explore → Plan → Implement → Verify

### 1. Explore
- If the task description provides app name and namespace, skip to Planning.
- Otherwise: check `/memories/app-operator/operations-log.md` for recent operations context.
- Use `list_applications` only if the target is completely unknown.
- For debugging: use `get_application_events` and `get_application_logs`.

### 2. Plan — MANDATORY for all mutations
- Present a clear summary of intended changes.
- Call `request_human_input` with a formatted plan (see sub-agent prompt for templates).
- Wait for user approval before proceeding.

### 3. Implement
- If missing any required parameter, call `request_human_input`.
- If a sync fails, use `get_application_events` and `get_application_logs` for root cause.

### 4. Verify
- Confirm the app reaches `Healthy` + `Synced` via `get_sync_status`.
- Do NOT declare success based solely on tool stdout.

## Workflow Reference

For detailed step-by-step workflow sequences, read `references/workflows.md`.

| User Intent | Workflow |
|---|---|
| Deploy new app end-to-end | `full_application_deployment` |
| Onboard a GitHub repo | `onboard_github_repository` |
| Debug a degraded app | `debug_application_issues` |
| Rollback a broken deploy | `rollback_decision` |
| Deploy new version | `deploy_new_version` |
| Set up multi-tenancy | `setup_argocd_project` |

## Safety Rules — MUST Follow

1. **Two-Layer HITL Model.** State-modifying operations have TWO safety layers:
   - **Layer 1 — Planning Gate**: Call `request_human_input` with a formatted plan summary.
   - **Layer 2 — Middleware Gate**: `HumanInTheLoopMiddleware` auto-pauses on gated tools.

2. **Diff before sync.** Run `get_application_diff` before `sync_application`.

3. **Dry-run for risky syncs.** For production or first-time deployments, use `dry_run=true` first.

4. **Disable auto-sync before rollback.** Check `get_application_details` for autosync.

5. **Never log credentials.** `GIT_PASSWORD`, `ARGOCD_AUTH_TOKEN`, `SSH_PRIVATE_KEY_PATH` are env-only.

6. **Validate before create.** Call `validate_application_config` first for `create_application`.

7. **Monitor after write.** Poll `get_sync_status` until completion.

8. **Missing Inputs Protection.** Never hallucinate parameters — call `request_human_input`.

## Agentic Defaults — Apply Automatically

- **`cluster_name`**: `"default"` when not specified.
- **`target_revision`**: `"HEAD"` when not specified.
- **Application name**: Derive from repo path (e.g., `charts/hello-world` → `hello-world`).
- **`destination_namespace`**: Use project's allowed destination, or `"default"` if wildcard.
- **`sync_policy`**: `"manual"` unless user explicitly requests auto-sync.
- **`repo_url` normalization**: Match the exact form from the project's `sourceRepos`.
- **`create_namespace`**: Set to `true` when creating or syncing applications if the target namespace does not already exist.

Always state derived defaults in the plan preview so the user can override.

## Response Format

- Lead with current status (health + sync) when showing app state.
- Use structured output (table or list) for multi-app results.
- For errors: provide root cause + immediate fixes + preventive measures.
