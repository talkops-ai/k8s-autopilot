# Kubernetes Cluster Operations — Workflows Reference

Detailed step sequences for common kubectl-equivalent operations. Load this file when executing any multi-step diagnostic or mutation workflow.

## Table of Contents
1. [Debug a Failing Pod](#1-debug-a-failing-pod)
2. [Cluster Health Check](#2-cluster-health-check)
3. [Apply or Update a Resource](#3-apply-or-update-a-resource)
4. [Scale a Deployment / StatefulSet](#4-scale-a-deployment--statefulset)
5. [Exec into a Running Pod](#5-exec-into-a-running-pod)
6. [Run a Temporary Debug Pod](#6-run-a-temporary-debug-pod)
7. [Investigate OOM / Resource Pressure](#7-investigate-oom--resource-pressure)
8. [Context Switch — Multi-Cluster](#8-context-switch--multi-cluster)
9. [Filter Pods by Label or Field](#9-filter-pods-by-label-or-field)
10. [Delete a Resource Safely](#10-delete-a-resource-safely)
11. [RBAC Inspection](#11-rbac-inspection)
12. [CrashLoopBackOff Diagnosis](#12-crashloopbackoff-diagnosis)

---

## 1. Debug a Failing Pod

**Trigger phrases:** "pod is failing", "pod not starting", "debug my pod", "pod in error state", "CrashLoopBackOff", "ImagePullBackOff", "pod stuck"

```
Step 1 → pods_list_in_namespace (namespace=<ns>)
         — Identify pod name; note status, restarts, age

Step 2 → pods_get (name=<pod>, namespace=<ns>)
         — Full spec: containers, image, resource limits, env, readiness/liveness probes
         — Key fields: status.conditions, status.containerStatuses[].state

Step 3 → pods_log (name=<pod>, namespace=<ns>, tail=200)
         — Scan for ERROR/WARN/FATAL/panic lines
         — If pod has restarted: pods_log (previous=true) to get last crash logs

Step 4 → events_list (namespace=<ns>)
         — Filter for WarningEvents related to pod name
         — Correlate: FailedScheduling, BackOff, OOMKilling, FailedMount, Unhealthy

Step 5 → Diagnose by status:

  CrashLoopBackOff:
    → Run workflow 12 for targeted diagnosis
    → Common: bad CMD/entrypoint, missing env, secret not found, port conflict

  ImagePullBackOff / ErrImagePull:
    → events_list — look for "failed to pull image" event
    → Check: image name typo, tag doesn't exist, registry auth (imagePullSecret)
    → resources_get (apiVersion=v1, kind=Secret, namespace=<ns>) — verify imagePullSecret

  Pending:
    → events_list — FailedScheduling, Unschedulable
    → Common: insufficient CPU/memory, no matching node (taint/toleration), PVC not bound
    → nodes_top — confirm node has capacity

  OOMKilled:
    → Run workflow 7 for full investigation

  CreateContainerConfigError:
    → Secret or ConfigMap referenced in pod spec doesn't exist
    → resources_get (v1/Secret or v1/ConfigMap, name=<name>, namespace=<ns>)

Step 6 → Report:
  📍 Pod: [name] in [namespace]
  🔴 Status: [phase/condition]
  📋 Errors: [log errors or event messages]
  🎯 Root Cause: [diagnosis]
  💡 Immediate Fix: [action]
  🛡️  Prevention: [recommendation]
```

---

## 2. Cluster Health Check

**Trigger phrases:** "cluster health", "is my cluster healthy", "cluster status", "overall cluster check"

**Option A — Use the built-in MCP prompt (recommended):**
```
Invoke: cluster-health-check prompt
  params: namespace (optional), check_events=true
  → Automatically runs a comprehensive multi-tool health assessment
  → Returns aggregated status across nodes, pods, events
```

**Option B — Manual sequence:**
```
Step 1 → namespaces_list
         — Identify all namespaces; note any unexpected ones

Step 2 → nodes_top
         — CPU+memory per node; flag >80% utilization
         → nodes_stats_summary (name=<node>) for any high-utilization node

Step 3 → pods_list (fieldSelector='status.phase=Pending')
         — All pending pods across cluster; should be 0 in healthy state

Step 4 → pods_list (fieldSelector='status.phase=Failed')
         — All failed pods; investigate any in production namespaces

Step 5 → events_list
         — Filter type=Warning; group by namespace+resource
         — Flag: OOMKilling, BackOff, FailedScheduling, NodeNotReady

Step 6 → resources_list (apiVersion=apps/v1, kind=Deployment)
         — List all Deployments; check AVAILABLE vs DESIRED replicas

Step 7 → Synthesize:
  Cluster Health Summary:
  ✅ Nodes: X/X Ready  |  CPU: XX%  |  Memory: XX%
  ⚠️  Pending Pods: N  |  Failed Pods: N
  📋 Warning Events: N (top 3: ...)
  🔴 Unhealthy Deployments: [list]
  Overall: [HEALTHY / AT-RISK / CRITICAL]
```

---

## 3. Apply or Update a Resource

**Trigger phrases:** "apply YAML", "create deployment", "update configmap", "kubectl apply", "deploy this manifest"

```
Step 1 → If resource already exists:
         resources_get (apiVersion=<api>, kind=<kind>, name=<name>, namespace=<ns>)
         — Show current state to user; confirm they want to overwrite

Step 2 → Show YAML to user:
         Present the complete manifest in a code block
         → "Review and confirm to apply:"
         → Get explicit confirmation before proceeding

Step 3 → resources_create_or_update (resource=<yaml_string>)
         — Upsert: creates if not exists, updates if exists (server-side apply semantics)

Step 4 → Verify:
         resources_get (apiVersion=<api>, kind=<kind>, name=<name>, namespace=<ns>)
         — Confirm resource exists and spec matches intent

Step 5 [For Deployments/StatefulSets] → Poll rollout:
         resources_list (apiVersion=apps/v1, kind=ReplicaSet,
                         label_selector='<app-label>', namespace=<ns>)
         — Confirm new ReplicaSet is ready
         pods_list_in_namespace (namespace=<ns>, label_selector='<app-label>')
         — Confirm pods are Running
```

**YAML string escaping:** When passing YAML as a string to `resources_create_or_update`, ensure proper escaping — newlines preserved, quotes escaped. Use the full YAML with `apiVersion`, `kind`, `metadata`, and `spec`.

---

## 4. Scale a Deployment / StatefulSet

**Trigger phrases:** "scale deployment", "increase replicas", "scale down", "zero replicas"

```
Step 1 → resources_scale (apiVersion=apps/v1, kind=Deployment, name=<name>, namespace=<ns>)
         — No scale param: READ ONLY — returns current replica count
         → Show to user: "Currently at N replicas. Scale to X?"

Step 2 → Confirm target replica count with user
         Special check: scale=0 means full shutdown — confirm explicitly

Step 3 → resources_scale (apiVersion=apps/v1, kind=Deployment, name=<name>,
                           namespace=<ns>, scale=<N>)
         — Executes the scale

Step 4 → pods_list_in_namespace (namespace=<ns>, label_selector='<app-label>')
         — Monitor: pods terminating (scale down) or new pods starting (scale up)

Step 5 → Confirm final state:
         resources_get (apiVersion=apps/v1, kind=Deployment, name=<name>, namespace=<ns>)
         — Verify .status.readyReplicas == target
```

**Scalable kinds:** `apps/v1 Deployment`, `apps/v1 StatefulSet`. Not applicable to DaemonSets (node-controlled).

---

## 5. Exec into a Running Pod

**Trigger phrases:** "exec into pod", "shell into pod", "run command in pod", "kubectl exec"

```
Step 1 → pods_get (name=<pod>, namespace=<ns>)
         — Confirm pod is Running; identify container name if multi-container

Step 2 → Confirm with user:
         "Executing [command] in pod [name]/[container] in namespace [ns]"
         — SAFETY: Treat as shell access. Never run destructive commands without explicit ask.

Step 3 → pods_exec (name=<pod>, namespace=<ns>, command=[...], container=<name>)
         command format: array — ["ls", "-la", "/app"] or ["sh", "-c", "env | grep DB"]

Common diagnostic commands:
  Check env vars:    ["env"]
  Check filesystem:  ["ls", "-la", "/app"]
  DNS resolution:    ["nslookup", "svc-name.namespace.svc.cluster.local"]
  Network reachable: ["wget", "-O-", "http://other-svc:port/health"]
  Config file:       ["cat", "/etc/config/app.yaml"]
  Process list:      ["ps", "aux"]
```

**Multi-container pods:** Always specify `container` param. Default exec target may be unpredictable.

---

## 6. Run a Temporary Debug Pod

**Trigger phrases:** "run debug pod", "temporary pod", "test connectivity", "run busybox", "ephemeral pod"

```
Step 1 → pods_run
         params:
           image: "busybox:latest" | "nicolaka/netshoot" | "curlimages/curl" | <user-specified>
           name: "debug-<timestamp>" (or let server generate random name)
           namespace: <ns>
           port: <port>  (optional — only if exposing)

Step 2 → pods_get (name=<pod>, namespace=<ns>)
         — Wait for Running state

Step 3 → pods_exec (name=<pod>, namespace=<ns>, command=[...])
         — Run diagnostic commands

Step 4 [Cleanup — ALWAYS] → pods_delete (name=<pod>, namespace=<ns>)
         — Delete debug pod after use. Confirm with user if they want to keep it.
```

**Recommended images by use case:**
- Network debugging: `nicolaka/netshoot` (tcpdump, nslookup, curl, dig, traceroute)
- HTTP testing: `curlimages/curl`
- General shell: `busybox:latest` or `alpine:latest`
- DNS testing: `tutum/dnsutils`

---

## 7. Investigate OOM / Resource Pressure

**Trigger phrases:** "OOMKilled", "out of memory", "resource pressure", "node under pressure", "memory limit exceeded"

```
Step 1 → pods_list (fieldSelector='status.phase=Failed')
         — Or pods_list_in_namespace with label_selector for the workload
         — Look for containers with lastState.terminated.reason=OOMKilled

Step 2 → pods_get (name=<pod>, namespace=<ns>)
         — Check: spec.containers[].resources.limits.memory
         — Check: status.containerStatuses[].lastState.terminated (exitCode, reason, finishedAt)

Step 3 → pods_top (namespace=<ns>, all_namespaces=false)
         — Check actual memory usage vs. configured limits

Step 4 → nodes_top
         — Identify if node is under memory pressure
         → nodes_stats_summary (name=<node>) for detailed memory breakdown
           (includes cgroup v2 PSI metrics if available: memory.pressure)

Step 5 → events_list (namespace=<ns>)
         — Look for: OOMKilling, Evicted, MemoryPressure events

Step 6 → Remediation options (present to user):
  a) Increase memory limit:
     resources_get (apps/v1/Deployment) → update resources.limits.memory → resources_create_or_update
  b) Optimize application memory usage (application-level)
  c) Enable VPA (VerticalPodAutoscaler) for automatic right-sizing
  d) Scale horizontally: resources_scale → increase replicas
```

---

## 8. Context Switch — Multi-Cluster

**Trigger phrases:** "switch cluster", "change context", "use production cluster", "which cluster am I on"

```
Step 1 → configuration_contexts_list
         — List all contexts: name, cluster, server URL, user
         → Present table to user; confirm target context

Step 2 → configuration_view (minified=true)
         — Show current active context and cluster endpoint

Note: kubernetes-mcp-server does NOT have a "set-context" tool.
      Context switching must be done at the kubeconfig level:
      → Instruct user to run: kubectl config use-context <context-name>
      → Or restart the MCP server with a different --kubeconfig pointing to the target cluster's config

For multi-cluster read operations within a single kubeconfig:
      → Some operations auto-use current context
      → For explicit cluster targeting, the --disable-multi-cluster flag must NOT be set
```

---

## 9. Filter Pods by Label or Field

**Trigger phrases:** "find pods by label", "get all pods with app=X", "pods on node", "running pods only"

```
Filter by label (app selector):
→ pods_list_in_namespace (namespace=<ns>, label_selector='app=myapp')
→ pods_list_in_namespace (namespace=<ns>, label_selector='app in (frontend,backend)')
→ pods_list (label_selector='env=production,tier=api')   ← cluster-wide

Filter by field (phase, node):
→ pods_list_in_namespace (namespace=<ns>, fieldSelector='status.phase=Running')
→ pods_list (fieldSelector='spec.nodeName=worker-node-1')
→ pods_list (fieldSelector='status.phase=Pending')       ← find all stuck pods

Combined label + field:
→ pods_list_in_namespace (namespace=<ns>,
                           label_selector='app=api',
                           fieldSelector='status.phase=Running')
```

**fieldSelector note:** `CrashLoopBackOff` is NOT a phase — it's a container state. To find crashlooping pods, use `pods_list` without fieldSelector and inspect `status.containerStatuses[].state.waiting.reason`.

---

## 10. Delete a Resource Safely

**Trigger phrases:** "delete deployment", "remove service", "kubectl delete", "clean up resources"

```
Step 1 → resources_get (apiVersion, kind, name, namespace)
         — Confirm resource exists; show to user
         → "About to delete [kind]/[name] in namespace [ns]. Confirm?"

Step 2 [For Deployments — check dependents]:
         resources_list (apiVersion=apps/v1, kind=ReplicaSet, namespace=<ns>,
                         label_selector=<same-app-label>)
         — Note: ReplicaSets and Pods are cascade-deleted automatically

Step 3 → resources_delete (apiVersion, kind, name, namespace)
         Optional: gracePeriodSeconds (omit for graceful; 0 for immediate)

Step 4 → Verify deletion:
         resources_get (same params)
         — Should return NotFound

Special cases:
  Stuck Terminating namespace → blocked by finalizers
    resources_get (v1, Namespace, name=<ns>) → check .metadata.finalizers
    resources_create_or_update with finalizers:[] to patch → removes finalizer

  Stuck Terminating pod:
    pods_delete (name=<pod>, namespace=<ns>, gracePeriodSeconds=0)
    — Force-delete; warn user this bypasses graceful shutdown
```

---

## 11. RBAC Inspection

**Trigger phrases:** "who has access", "check RBAC", "list roles", "what can this serviceaccount do"

```
List Roles in namespace:
→ resources_list (apiVersion=rbac.authorization.k8s.io/v1, kind=Role, namespace=<ns>)

List ClusterRoles:
→ resources_list (apiVersion=rbac.authorization.k8s.io/v1, kind=ClusterRole)

Get specific Role:
→ resources_get (apiVersion=rbac.authorization.k8s.io/v1, kind=Role, name=<name>, namespace=<ns>)

List RoleBindings (who has which role):
→ resources_list (apiVersion=rbac.authorization.k8s.io/v1, kind=RoleBinding, namespace=<ns>)

Get ServiceAccount:
→ resources_get (apiVersion=v1, kind=ServiceAccount, name=<name>, namespace=<ns>)
```

---

## 12. CrashLoopBackOff Diagnosis

**Trigger phrases:** "CrashLoopBackOff", "container keeps restarting", "restart count is high"

```
Step 1 → pods_get (name=<pod>, namespace=<ns>)
         Key fields:
           status.containerStatuses[].restartCount    → how many times
           status.containerStatuses[].lastState.terminated.exitCode → exit code
           status.containerStatuses[].lastState.terminated.reason   → OOMKilled / Error / Completed

Step 2 → pods_log (name=<pod>, namespace=<ns>, previous=true, tail=200)
         — Logs from the LAST crash (before current restart)
         — Most revealing: final lines before exit

Step 3 → pods_log (name=<pod>, namespace=<ns>, tail=50)
         — Current container logs

Step 4 → Diagnose by exit code:
  Exit 0:  Process completed — check if it's a one-shot Job run as Deployment
  Exit 1:  Application error — read logs for exception/panic
  Exit 2:  Misuse of shell / command not found
  Exit 126: Command not executable
  Exit 127: Command not found in container PATH
  Exit 137: OOMKilled (SIGKILL) — run workflow 7
  Exit 139: Segfault (SIGSEGV) — application crash / memory corruption
  Exit 143: Graceful shutdown (SIGTERM) — liveness probe failing causing restart

Step 5 → Check probes (if exit 143):
         pods_get → spec.containers[].livenessProbe (path, port, initialDelaySeconds)
         → If probe is too aggressive: suggest increasing initialDelaySeconds or failureThreshold
```

---

## 13. OpenShift-Specific Resources

When connected to an OpenShift cluster, use these additional apiVersion+kind combinations:

| Kind | apiVersion | Notes |
|------|-----------|-------|
| Route | `route.openshift.io/v1` | OpenShift equivalent of Ingress |
| Project | `project.openshift.io/v1` | OpenShift namespace wrapper |
| DeploymentConfig | `apps.openshift.io/v1` | Legacy; prefer Deployment |
| BuildConfig | `build.openshift.io/v1` | OpenShift source-to-image builds |
| ImageStream | `image.openshift.io/v1` | Internal image registry management |
| SecurityContextConstraints | `security.openshift.io/v1` | OpenShift pod security policies |

Use `projects_list` instead of `namespaces_list` on OpenShift clusters.

---

## 14. Node Log Query Patterns

For `nodes_log` tool — the `query` param accepts systemd unit names or log file paths:

```
# System service logs (systemd unit names)
query: "kubelet"
query: "crio"
query: "containerd"
query: "kube-proxy"

# Log file paths
query: "/var/log/messages"
query: "/var/log/syslog"
query: "/var/log/kern.log"
```

Common use: `nodes_log(name=<node>, query="kubelet", tailLines=100)` — inspect kubelet logs for scheduling or eviction issues.
