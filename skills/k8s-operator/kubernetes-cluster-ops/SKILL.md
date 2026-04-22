---
name: kubernetes-cluster-ops
description: >-
  Use when the user asks to list, get, create, update, or delete any
  Kubernetes resource; inspect pod logs; exec into pods; run temporary pods;
  check resource usage (top); manage namespaces; view cluster events;
  troubleshoot degraded workloads; scale deployments or statefulsets; or manage
  kubeconfig contexts. Also use when the user reports a pod crashing, a
  deployment stuck, or a node under pressure — even if they don't mention
  kubectl by name. Triggers on keywords: kubectl, namespace, pod logs, exec
  into pod, describe pod, deployment, get pods, apply YAML, delete resource,
  scale deployment, cluster events, kubeconfig, context switch, top pods,
  top nodes, CrashLoopBackOff, OOMKilled, pending pod, ImagePullBackOff,
  cluster health, node stats.
metadata:
  author: talkops.ai
  version: '3.0'
  mcp_server: kubernetes-mcp-server
  upstream: https://github.com/containers/kubernetes-mcp-server
compatibility: >-
  Requires kubernetes-mcp-server (Go binary, npm, or container) with config+core
  toolsets enabled. Kubernetes cluster access via kubeconfig or in-cluster config.
---

# Kubernetes Cluster Operations Skill

## When to Use

Load this skill for any **state-modifying** Kubernetes operation: creating or updating
resources via YAML, scaling deployments/statefulsets, deleting resources or pods, exec-ing
into containers, or running temporary debug pods.

Read-only queries (list pods, get resources, pod logs, top, events, contexts, health check)
do NOT need this skill — the sub-agent handles those directly via the Read-Only Fast-Path
without loading any files.

## MCP Server Context

All tools are provided by the **kubernetes-mcp-server** (MCP name: `io.github.containers.kubernetes-mcp-server`).
Tool names are called without namespace prefix — registered directly (e.g., `pods_list`, `resources_get`).

**Active toolsets (default):** `config` + `core`. All workflows in this skill use these two.
**Optional toolsets:** `helm`, `tekton`, `kiali`, `kubevirt`, `kcp` — loaded separately if needed.

**Server modes — know before acting:**
- `--read-only` → No create/update/delete/exec/scale allowed. Read-only tools only.
- `--disable-destructive` → Blocks delete and update; create is allowed.
- No flag → Full access. Confirm write intent before any mutation.

**Multi-cluster `context` param:** When multi-cluster is enabled (default), all tools accept
an optional `context` param to target a specific cluster. If omitted, the current kubeconfig
context is used.

**Validation layer** (if `validation_enabled=true`): Catches typos in Kind/apiVersion, schema
errors, and RBAC denials before they reach the Kubernetes API.

## MCP Resources

Resource URIs are listed in the system prompt's Read-Only Fast-Path table — do not duplicate here.

## Core Workflow: Explore → Plan → Execute → Verify

### 1. Explore
- If the task provides resource kind, name, namespace, and action, skip to Planning.
- Otherwise: check `/memories/k8s-operator/operations-log.md` for recent operations.
- Use read-only tools to discover current state.

### 2. Plan — MANDATORY for all mutations

**Idempotency: NEVER create a resource without first checking if it already exists.**

| Before creating... | First check with... | If exists... |
|---|---|---|
| Any resource | `resources_get(apiVersion, kind, name, namespace)` | Use `resources_create_or_update` (upsert) — warn user |
| Pod (via `pods_run`) | `pods_get(name, namespace)` | Report existing pod |
| Scale target | `resources_scale` (no `scale` param) | Read current replicas first |

- Always read before write — call the read variant first.
- Present clear action plan and confirm with user.

### 3. Execute
- Tools are additionally gated by `HumanInTheLoopMiddleware`.
- For `resources_create_or_update`: show YAML before applying.
- For `pods_exec`: confirm exact command before running.

### 4. Verify
- After mutation, re-read the resource to confirm the change took effect.
- For scale: confirm `readyReplicas` matches target.
- For delete: confirm resource is gone (404 is expected).
- Do NOT declare success based solely on tool stdout.

## Tool Reference

### Config Tools (3 tools)

| Tool | kubectl equiv | Purpose |
|------|-------------|---------|
| `configuration_contexts_list` | `kubectl config get-contexts` | List all contexts + server URLs |
| `configuration_view` | `kubectl config view` | Kubeconfig YAML; `minified=true` for current only |
| `targets_list` | — | List multi-cluster targets |

### Pod Tools (8 tools)

| Tool | Write? | Key params |
|------|--------|-----------|
| `pods_list` | No | `label_selector`, `fieldSelector` |
| `pods_list_in_namespace` | No | `namespace`*, `label_selector`, `fieldSelector` |
| `pods_get` | No | `name`*, `namespace` |
| `pods_delete` | Yes | `name`*, `namespace` |
| `pods_log` | No | `name`*, `namespace`, `container`, `tail` (default 100), `previous` |
| `pods_top` | No | `name`, `namespace`, `label_selector`, `all_namespaces` |
| `pods_exec` | Yes | `name`*, `command`* (array), `container`, `namespace` |
| `pods_run` | Yes | `image`*, `name`, `namespace`, `port` |

### Generic Resource Tools (5 tools)

| Tool | Write? | Key params |
|------|--------|-----------|
| `resources_list` | No | `apiVersion`*, `kind`*, `namespace`, `label_selector`, `fieldSelector` |
| `resources_get` | No | `apiVersion`*, `kind`*, `name`*, `namespace` |
| `resources_create_or_update` | Yes | `resource`* (YAML or JSON string) |
| `resources_delete` | Yes | `apiVersion`*, `kind`*, `name`*, `namespace`, `gracePeriodSeconds` |
| `resources_scale` | Yes (if `scale` provided) | `apiVersion`*, `kind`*, `name`*, `namespace`, `scale` |

### Namespace & Cluster Tools (2 tools)

| Tool | Purpose |
|------|---------|
| `namespaces_list` | List all namespaces (or `projects_list` for OpenShift) |
| `events_list` | Cluster events; `namespace` param to scope |

### Node Tools (3 tools)

| Tool | Purpose |
|------|---------|
| `nodes_top` | CPU+memory per node |
| `nodes_stats_summary` | Deep per-node stats: CPU, mem, filesystem, network |
| `nodes_log` | Fetch kubelet/kube-proxy logs; `query`* = service name |

### MCP Prompt

| Prompt | Purpose |
|--------|---------|
| `cluster-health-check` | Comprehensive cluster health assessment (safe, read-only) |

## Workflow Routing

Load `references/workflows.md` ONLY when executing a multi-step workflow from the table below.
Read ONLY the section matching the selected workflow — do NOT load the entire file.

| User Intent | Workflow Section |
|-------------|---------|
| Inspect a failing pod | `#debug-failing-pod` |
| Cluster-wide health check | `#cluster-health-check` |
| Apply or update a resource | `#apply-resource` |
| Scale a deployment | `#scale-resource` |
| Exec into a running pod | `#exec-pod` |
| Run a temporary debug pod | `#run-debug-pod` |
| Investigate OOM / resource pressure | `#resource-pressure` |
| Switch cluster context | `#context-switch` |
| Find pods by label or field | `#filter-pods` |

## apiVersion / Kind Quick Reference

Common pairings (commit to memory). For YAML skeletons and OpenShift-specific resources,
load `references/workflows.md` → sections `#13-openshift-specific-resources` and `#3-apply-or-update-a-resource`.

| Resource | apiVersion | kind |
|----------|-----------|------|
| Pod | `v1` | `Pod` |
| Service | `v1` | `Service` |
| ConfigMap | `v1` | `ConfigMap` |
| Secret | `v1` | `Secret` |
| Deployment | `apps/v1` | `Deployment` |
| StatefulSet | `apps/v1` | `StatefulSet` |
| DaemonSet | `apps/v1` | `DaemonSet` |
| Ingress | `networking.k8s.io/v1` | `Ingress` |
| CronJob | `batch/v1` | `CronJob` |
| Job | `batch/v1` | `Job` |
| HPA | `autoscaling/v2` | `HorizontalPodAutoscaler` |
| PVC | `v1` | `PersistentVolumeClaim` |
| Role | `rbac.authorization.k8s.io/v1` | `Role` |
| ClusterRole | `rbac.authorization.k8s.io/v1` | `ClusterRole` |
| CRD | `apiextensions.k8s.io/v1` | `CustomResourceDefinition` |

## Safety Rules — MUST Follow

1. **Never mutate without confirmation in production namespaces.** Before any
   `resources_create_or_update`, `resources_delete`, `pods_delete`, or `resources_scale` in
   namespaces `production`, `prod`, `default` (hosting workloads), or `kube-system` — confirm
   intent with the user. State exactly what will be changed.

2. **Always read before write.** Before scaling or updating, call the read variant first
   (`resources_get` or `resources_scale` without `scale` param). Present current state to user.

3. **Never delete system resources.** Do not call `resources_delete` on resources in
   `kube-system`, `kube-public`, or `kube-node-lease` without explicit user instruction. Same
   for Node objects.

4. **`pods_exec` is a shell foothold.** Confirm target pod, namespace, and exact command.
   Never run destructive commands inside pods without explicit instruction.

5. **gracePeriodSeconds=0 is force-kill.** Only pass `gracePeriodSeconds=0` when user explicitly
   says "force delete" or pod is stuck terminating. Note: `pods_delete` does NOT accept
   `gracePeriodSeconds` — use `resources_delete(apiVersion='v1', kind='Pod', ...)` instead.

6. **`pods_run` creates real pods.** Confirm image, namespace, and cleanup intent. Suggest
   `--restart=Never` semantics for one-shot debugging.

7. **Verify cluster context before multi-cluster writes.** Call `configuration_contexts_list`
   first. Confirm correct context before any write operation.

8. **`resources_create_or_update` is an upsert.** It overwrites existing resources if
   name+namespace match. Show YAML and confirm before applying.

9. **Secrets: never display data values.** Acknowledge key names but mask values. The server
   can also block Secret access via `[[denied_resources]]` in TOML config.

## Gotchas

- **`fieldSelector` for pods is limited.** Only `status.phase`, `spec.nodeName`,
  `metadata.name`, `metadata.namespace`, `spec.serviceAccountName` are supported. You CANNOT
  filter by `CrashLoopBackOff` — it's a container state, not a pod field. Use `pods_list` then
  inspect status manually.
- **`resources_scale` dual mode.** Call WITHOUT `scale` param = read-only (returns current
  replicas). Call WITH `scale` param = mutation (sets replicas). Always read first.
- **Force-delete path is through `resources_delete`, not `pods_delete`.** Only
  `resources_delete` accepts `gracePeriodSeconds`. Calling `pods_delete` for a stuck pod
  will use graceful termination (default 30s) which won't help.
- **`resources_create_or_update` uses server-side apply.** Field ownership conflicts may
  occur if other controllers (e.g., HPA) manage the same fields. Watch for "conflict" errors.
- **`nodes_log` requires node name, not pod name.** Passing a pod name will return empty
  results without error. Always resolve the node first via `pods_get` → `spec.nodeName`.
- **`events_list` returns ALL event types by default.** For debugging, filter for
  `type=Warning` events — `Normal` events are usually noise during incident investigation.

## Response Format

- For list operations: use a table (name, namespace, status, age); summarize counts at bottom.
- For pod logs: highlight ERROR/WARN/FATAL lines; provide root cause analysis when errors found.
- For `pods_top` / `nodes_top`: present as sorted table; flag any container using >80% of limit.
- For events: group by `type=Warning` first, then `Normal`; correlate with resource names.
- For `cluster-health-check` prompt: lead with summary score (healthy / at-risk / critical).
- For YAML operations: show YAML in a code block with "Review and confirm" prompt.
