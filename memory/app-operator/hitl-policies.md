# Kubernetes Autopilot HITL Policies

> **Read-only.** This file is auto-injected into every coordinator model call. Do not modify via `edit_file`.

This file is the **authoritative declaration** of which tools require HITL approval.
It must exactly match the `interrupt_on` configs in middleware factories.

## 1. Explicit Approval Required (Gate)

The following tools trigger a `HumanInTheLoopMiddleware` interrupt requiring explicit `approve` or `reject`:

### ArgoCD (9 gates — `build_app_operator_hitl_middleware`)
- `create_application` — new ArgoCD Application
- `update_application` — modify existing Application
- `sync_application` — trigger Application sync
- `delete_application` — remove Application
- `delete_project` — remove ArgoCD Project
- `delete_repository` — remove repository connection
- `onboard_repository_https` — register HTTPS repository
- `onboard_repository_ssh` — register SSH repository
- `create_project` — new ArgoCD Project

### Argo Rollouts (10 gates — `build_argo_rollouts_hitl_middleware`)
- `argo_delete_rollout` — remove Rollout CRD + ReplicaSets
- `argo_delete_experiment` — tear down experiment pods
- `convert_deployment_to_rollout` — Deployment → Rollout migration
- `convert_rollout_to_deployment` — reverse migration
- `argo_manage_rollout_lifecycle` — promote / promote_full / abort / skip_analysis / pause / resume
- `argo_manage_legacy_deployment` — direct legacy Deployment mutation
- `argo_create_rollout` — create new Rollout CRD
- `argo_configure_analysis_template` — create/apply AnalysisTemplate
- `create_stable_canary_services` — create stable + canary Services
- `argo_update_rollout` — update rollout image/spec (live workload mutation)

### Traefik (7 gates — `build_traefik_hitl_middleware`)
- `traefik_manage_weighted_routing` — canary traffic split
- `traefik_manage_simple_route` — create/modify IngressRoute
- `traefik_manage_middleware` — create/modify Traefik Middleware
- `traefik_nginx_migration` — NGINX → Traefik migration (action=apply/revert)
- `traefik_manage_tcp_routing` — TCP/TLS routing
- `traefik_configure_service_affinity` — sticky sessions
- `traefik_generate_routing_manifest` — generate and apply routing manifest

### Helm (4 gates — `build_helm_hitl_middleware`)
- `helm_install_chart` — install new Helm release
- `helm_upgrade_release` — upgrade existing release
- `helm_rollback_release` — rollback to previous revision
- `helm_uninstall_release` — remove release and resources

### Execution Protocol
When planning gated operations, you MUST:
1. Explain the blast radius of the change.
2. Present the exact tool input you intend to use.
3. The middleware will pause execution automatically — do not bypass.

## 2. Default Safe Operations (No Gate)

The following are **not** gated and can run autonomously:

- **All MCP resources** (`read_mcp_resource`) — read-only status, health, metrics, history
- **Local file operations** — writing, validating templates and values on the virtual filesystem
- **Argo Rollouts autonomous promotion** — `argo_manage_rollout_lifecycle(action='promote')` when traffic weight is **≤ 50%** and AnalysisRuns are passing (rule #7 in AGENTS.md)
- **Argo Rollouts validation** — `validate_deployment_ready` (read-only pre-flight check)
- **ArgoCD ignore-differences** — `generate_argocd_ignore_differences` (generates YAML only)

## 3. Plan-Aware Auto-Approval

When a plan is locked in state (`PlanLockMiddleware` detects active plan in `state["files"]`) and the coordinator delegates with `[PLAN-LOCKED]` prefix:
- The user has already reviewed and approved the exact parameters at the coordinator level.
- The `HumanInTheLoopMiddleware` still fires for all gated tools (this is the mechanical safety net).
- Future enhancement: auto-approve when tool args match the plan parameters exactly, eliminating dual-approval fatigue while maintaining deviation detection.

## 4. Tool Review Mode

For operations not in §1 but where the user context is uncertain or sensitive (e.g., cross-namespace mutation):
- Use `request_human_input` to ask for confirmation before proceeding.

## 5. Coordinator-Level Approval Gate (PATH A — Mandatory)

The following operations MUST go through `write_todos` + `request_user_input` at the
coordinator level before delegating to a sub-agent (PATH A):

### ArgoCD coordinator gates
- Any `sync_application` — state-modifying on live app, especially production namespaces
- Any `delete_application` / `delete_project` / `delete_repository` — destructive, irreversible
- Any `create_application` / `update_application` / `create_project` — new resource creation

### Argo Rollouts coordinator gates
- `argo_manage_rollout_lifecycle` with action=`abort`, `promote_full`, `skip_analysis`, `pause`
- Any rollback operation (even to a named revision — blast radius discovery required)
- `convert_deployment_to_rollout` / `convert_rollout_to_deployment` — live workload migration
- `argo_delete_rollout` / `argo_delete_experiment`

### Traefik coordinator gates
- Any traffic weight change (`traefik_manage_weighted_routing`) — live traffic mutation
- `traefik_nginx_migration` — infrastructure migration
- `traefik_manage_simple_route` (create/modify) — live routing change

### Exempt from coordinator gate (PATH B — DIRECT EXECUTE permitted)
- Read-only operations: list, status, get, describe (any sub-agent)
- Health checks and validation (non-mutating)

### Note on dual-gate architecture
The coordinator-level gate (semantic review — "what are we doing and why?") is distinct
from the sub-agent tool-level `HumanInTheLoopMiddleware` (mechanical safety net — "exact
tool args approval"). Both fire for PATH A operations. Only the middleware fires for PATH B.
