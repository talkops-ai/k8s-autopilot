# App Operator Agent Memory

> **Read-only.** Do not modify via `edit_file`. Always injected into every coordinator model call.

## Core Directives
1. Never assume deletions or syncing new resources are safe — always get HITL approval for destructive/state-modifying ops.
2. Never ignore validators — surface exact errors, never silently suppress.
3. Follow all coordination rules rigidly when handling cross-domain operations (e.g., Traefik + Argo Rollouts).

## Operations Journal — Context Persistence
After every state-modifying app operation, the coordinator MUST call `log_app_operation` to persist context to `/memories/app-operator/operations-log.md`. The tool's typed parameters define the required fields — follow the tool schema.

### Context Recovery Rules
1. The `AppOperationContextMiddleware` auto-injects recent operations before every model call.
2. For ArgoCD follow-ups, the coordinator MUST include full app name, namespace, and repo details in the task description.
3. For Argo Rollouts follow-ups, the coordinator MUST include rollout name, namespace, current strategy, phase, and canary step index in the task description.
4. For Traefik follow-ups, the coordinator MUST include route name, namespace, current weights, and affected services in the task description.
5. Sub-agents MUST check the task description and operations journal BEFORE asking the user for any parameter. User input is a LAST RESORT.

## Coordination Rules (Cross-Domain)
1. **App Sync Before Rollout Wait**: If you created a Rollout CR via Git commit, sync the ArgoCD application FIRST before attempting to promote or check rollout status.
2. **Reverse Migration**: To revert to a Deployment, use `convert_rollout_to_deployment` — do not manually delete and recreate.
3. **Traefik + Argo Rollouts Canary**: Use Traefik for L7 traffic splitting at the edge; use Argo Rollouts for pod-level progressive delivery. Do NOT use both on the same service without user confirmation.
4. **Traefik + ArgoCD**: When creating Traefik CRDs via GitOps, ensure the ArgoCD Application includes `ignoreDifferences` for Traefik-managed annotations to prevent sync thrashing.
5. **NGINX Migration**: Before applying NGINX-to-Traefik migration, verify no ArgoCD Application manages the target Ingress resources.
6. **Autonomous Promotion Ceiling**: Rollouts sub-agent may autonomously promote canary steps up to 50% traffic. At ≥50%, MUST pause for human approval. `promote_full` always requires explicit approval.
7. **Rollout-specific safety rules** (workloadRef integration gate, abort recovery protocol, AnalysisTemplate prerequisites, Inconclusive handling): see `SKILL.md` in `argo-rollouts-gitops`.
8. **Traefik-specific safety rules** (generate-before-apply, weight zeroing protection, TCP no-rollback, sourceCriterion for rate limits, ACME interception): see `SKILL.md` in `traefik-edge-routing`.
