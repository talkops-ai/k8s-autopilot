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

## Parameter Completeness — Anti-Hallucination Policy
1. **NEVER guess resource names.** If the user's request or operations journal does not contain the exact deployment/rollout/application/route name, the coordinator MUST resolve it before delegating mutation tasks.
2. **Resolution order**: (a) operations journal auto-context → (b) delegate a READ-ONLY list/discovery task to the sub-agent → (c) present discovered resources to the user for selection → (d) ask the user directly as last resort.
3. **404 = wrong name = STOP.** If any sub-agent tool returns "not found" (404), the sub-agent MUST immediately stop and return. Do NOT retry with alternative names.
4. **Max 2 failed lookups per resource.** If the same tool fails twice with "not found" for different names, STOP and return requesting clarification.

### Required Parameters by Operation

| Operation | Required Params | Can Auto-Discover? |
|---|---|---|
| ArgoCD app operations | app_name | Yes — `list_applications` |
| Argo Rollouts migration | deployment_name + namespace | Yes — coordinator lists deployments via sub-agent |
| Argo Rollouts lifecycle | rollout_name + namespace | Yes — `argorollout://rollouts/list` |
| Traefik route operations | route_name + namespace | Yes — `traefik://traffic/routes/list` |

## Coordination Rules (Cross-Domain)
1. **App Sync Before Rollout Wait**: If you created a Rollout CR via Git commit, sync the ArgoCD application FIRST before attempting to promote or check rollout status.
2. **Reverse Migration**: To revert to a Deployment, use `convert_rollout_to_deployment` — do not manually delete and recreate.
3. **Traefik + Argo Rollouts Canary**: Use Traefik for L7 traffic splitting at the edge; use Argo Rollouts for pod-level progressive delivery. Do NOT use both on the same service without user confirmation.
4. **Traefik + ArgoCD**: When creating Traefik CRDs via GitOps, ensure the ArgoCD Application includes `ignoreDifferences` for Traefik-managed annotations to prevent sync thrashing.
5. **NGINX Migration**: Before applying NGINX-to-Traefik migration, verify no ArgoCD Application manages the target Ingress resources.
6. **Autonomous Promotion Ceiling**: Rollouts sub-agent may autonomously promote canary steps up to 50% traffic. At ≥50%, MUST pause for human approval. `promote_full` always requires explicit approval.
7. **Rollout-specific safety rules** (workloadRef integration gate, abort recovery protocol, AnalysisTemplate prerequisites, Inconclusive handling): see `SKILL.md` in `argo-rollouts-gitops`.
8. **Traefik-specific safety rules** (generate-before-apply, weight zeroing protection, TCP no-rollback, sourceCriterion for rate limits, ACME interception): see `SKILL.md` in `traefik-edge-routing`.

## Plan Immutability — Intent Lock Protocol
1. **Once approved, execution MUST match the plan.** The coordinator stores the plan as a structured artifact. The `PlanLockMiddleware` re-injects it before every model call and validates intent alignment. Deviations are blocked.
2. **Plan lifecycle**: Created at coordinator → Approved by user → Locked in state → Sub-agent reads as constraint → Middleware validates → Cleared after execution.
3. **Plan-locked delegation format**: `[STATE-MODIFYING] [PLAN-LOCKED] {exact parameters}`. Sub-agents receiving this prefix MUST NOT re-plan or modify parameters.

## Rejection Protocol
1. If the user REJECTS a plan, the sub-agent MUST stop immediately and return to the coordinator.
2. Do NOT retry with a modified plan — the coordinator handles re-engagement.
3. Maximum 2 plan presentations per user request. After 2 rejections, ask the user to rephrase their requirements.

## Deviation Reporting
1. If execution results differ from the plan (e.g., tool returned unexpected values), report the discrepancy explicitly.
2. Include: planned parameters, actual results, specific deviations.
3. The operations journal MUST log both the planned and actual values for auditability.

## Intent Translation (for non-DevOps users)
1. The coordinator MUST translate natural language to DevOps parameters before delegation. See the Intent Translation table in the coordinator prompt.
2. When user intent is ambiguous, present options: "Did you mean X or Y?" — do NOT guess.
3. NEVER assume the user knows Kubernetes/ArgoCD terminology. Present plans in plain English with blast-radius warnings for production namespaces.
