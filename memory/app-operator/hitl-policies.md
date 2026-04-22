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

### Argo Rollouts (9 gates — `build_argo_rollouts_hitl_middleware`)
- `argo_delete_rollout` — remove Rollout CRD + ReplicaSets
- `argo_delete_experiment` — tear down experiment pods
- `convert_deployment_to_rollout` — Deployment → Rollout migration
- `convert_rollout_to_deployment` — reverse migration
- `argo_manage_rollout_lifecycle` — promote / promote_full / abort / skip_analysis / pause / resume
- `argo_manage_legacy_deployment` — direct legacy Deployment mutation
- `argo_create_rollout` — create new Rollout CRD
- `argo_configure_analysis_template` — create/apply AnalysisTemplate
- `create_stable_canary_services` — create stable + canary Services

### Traefik (6 gates — `build_traefik_hitl_middleware`)
- `traefik_manage_weighted_routing` — canary traffic split
- `traefik_manage_simple_route` — create/modify IngressRoute
- `traefik_manage_middleware` — create/modify Traefik Middleware
- `traefik_nginx_migration` — NGINX → Traefik migration (action=apply/revert)
- `traefik_manage_tcp_routing` — TCP/TLS routing
- `traefik_configure_service_affinity` — sticky sessions

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

## 3. Tool Review Mode

For operations not in §1 but where the user context is uncertain or sensitive (e.g., cross-namespace mutation):
- Use `request_human_input` to ask for confirmation before proceeding.
