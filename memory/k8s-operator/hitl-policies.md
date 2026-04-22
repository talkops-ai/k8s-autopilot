# K8s Operator HITL Policies

This file outlines the human-in-the-loop (HITL) policies for the K8s Operator, determining when an agent MUST pause and wait for explicit human approval.

## 1. Explicit Approval Required (Gate)

The following operations are considered **destructive or high-risk** and require explicit `approve` or `reject` from a human:

*   **Resource Deletion**: `resources_delete` — permanently removes any K8s resource from the cluster
*   **Pod Deletion**: `pods_delete` — terminates a running pod
*   **Resource Upsert**: `resources_create_or_update` — server-side apply that can overwrite existing resources
*   **Scaling**: `resources_scale` (when `scale` param is provided) — changes replica count; scale-to-zero shuts down service
*   **Pod Exec**: `pods_exec` — shell-level access to containers; equivalent to SSH
*   **Pod Run**: `pods_run` — creates real pods consuming cluster resources

### Elevated Caution Namespaces
The following namespaces trigger additional warnings in approval cards:
*   **Production**: `production`, `prod`, `live`, `prd`
*   **System**: `kube-system`, `kube-public`, `kube-node-lease`

### Execution
When planning these operations, you MUST:
1. Explain the blast radius of the change.
2. Present the exact tool input you intend to use.
3. Pause execution by calling the relevant tool where `interrupt_on` config is set.

## 2. Default Safe Operations (No Gate)

The following operations are considered **safe** and can be run autonomously:

*   **Read-Only Operations**: `pods_list`, `pods_list_in_namespace`, `pods_get`, `pods_log`, `pods_top`, `resources_list`, `resources_get`, `resources_scale` (without `scale` param — read-only mode), `namespaces_list`, `events_list`, `nodes_top`, `nodes_stats_summary`, `nodes_log`, `configuration_contexts_list`, `configuration_view`, `targets_list`
*   **Cluster Health Check**: The `cluster-health-check` MCP prompt is safe — it only reads data.

## 3. Tool Review Mode

For operations that do not inherently pause but where the user context is uncertain or sensitive (e.g. cross-namespace mutation, unfamiliar cluster context):
- Use `request_human_feedback` when in doubt.
