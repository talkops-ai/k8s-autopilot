# K8s Operator Agent Memory Index

This directory serves as the virtual filesystem `/memories/k8s-operator/` seeded into all K8s Operator sub-agents.
The coordinator reads this file at session start to provide global context to the agent fleet.

## Sub-Agent Roster

| Agent Name | Primary MCP | Responsibilities |
|------------|-------------|------------------|
| **k8s-cluster-ops** | `kubernetes_mcp_server` | Resource CRUD, pod debugging, scaling, exec, events, node diagnostics, cluster health, context management. |

## Execution Safety Rules
- **NEVER** bypass HITL tools for destructive operations
- **NEVER** mutate resources in `kube-system`, `kube-public`, or `kube-node-lease` without explicit user instruction
- **ALWAYS** read before write — `resources_get` or `resources_scale` (no scale param) first
- **ALWAYS** confirm target cluster context in multi-cluster environments before write operations
- For `kind=Secret`: acknowledge keys but **NEVER** display data values in plain text

## Context Recovery Rules
1. **Operations Journal**: Check `/memories/k8s-operator/operations-log.md` for recent operations before asking the user to repeat context. The journal persists across summarization.
2. **Cross-Domain Awareness**: The K8s Operator and App Operator may both target the same cluster resources (e.g., Deployments that Argo Rollouts also manages). Before modifying a Deployment, check if it is referenced by a Rollout (`workloadRef`). If so, recommend using the App Operator's Argo Rollouts sub-agent instead.
3. **Safety Rules Source**: Detailed safety rules and tool reference are in `SKILL.md` — do NOT duplicate them here. Load SKILL.md only for state-modifying operations.

## Coordination Rules
1. **Read-Before-Write Pattern**:
   Before any mutation (`resources_create_or_update`, `resources_scale`, `resources_delete`), always perform a read first and present current state to the user.
2. **Force Delete Awareness**:
   `gracePeriodSeconds=0` is force-kill. Only use when user explicitly says "force delete" AND the pod is stuck terminating. Use `resources_delete` (not `pods_delete`) to pass `gracePeriodSeconds`.
3. **Debug Pod Cleanup**:
   Any pod created via `pods_run` must be deleted after use. Always confirm cleanup intent with the user.
4. **Cluster Health Check**:
   Prefer the built-in `cluster-health-check` MCP prompt over manual multi-tool sequences for comprehensive assessments.

## Environment Context
- Multi-cluster via kubeconfig contexts. Use `configuration_contexts_list` before cross-cluster operations.
- Server modes: `--read-only` blocks all mutations; `--disable-destructive` blocks delete/update only.
- Validation layer catches Kind/apiVersion typos and RBAC denials before hitting the K8s API.
