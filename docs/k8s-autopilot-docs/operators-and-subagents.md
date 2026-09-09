# Operators and sub-agents

> How k8s-autopilot breaks down your requests into specialized teams of operators

When you ask k8s-autopilot something, it doesn't try to do everything itself. Instead, it works like a team — a central coordinator (the **Supervisor**) listens to your request, figures out which domain it belongs to, and hands it off to the right **Operator**. Each operator is a specialist: it knows its tools, its safety rules, and how to get the job done.

Think of it like a hospital. You walk in and describe your symptoms to the front desk (the Supervisor). They don't try to treat you — they send you to the right department: cardiology, radiology, or surgery. Each department has its own specialists, equipment, and procedures.

## How it works

```
You (chat) → Supervisor → Operator → MCP Server(s) → Kubernetes / external systems
```

1. **You** type a request in the chat interface.
2. The **Supervisor** reads your request and decides which operator should handle it.
3. The **Operator** loads the right skill playbook, connects to its MCP server(s), and executes the task.
4. The **MCP Server** talks to the actual external system (your Kubernetes cluster, Helm, ArgoCD, Prometheus, etc.) and performs the real work.

For anything that changes your system (installing software, scaling pods, creating routes), every operator follows the same safety pattern: **Discover → Plan → Confirm → Execute → Verify**. Nothing gets changed without showing you the plan first and asking for your approval.

---

## The four operators

k8s-autopilot ships with four built-in operators. Each one is a self-contained agent with its own MCP server connections, skills, and safety rules.

### Helm Operator

**What it does:** Manages the full lifecycle of Helm charts on your Kubernetes cluster — finding charts, installing them, upgrading releases, rolling back to previous versions, and removing releases when you're done.

**When the Supervisor routes to it:** When you mention anything about Helm charts, releases, installing software from a chart repository, upgrading a deployed release, or rolling back a failed deployment.

**MCP Server:** `helm-mcp-server` — provides 22 tools covering both Helm operations and basic Kubernetes awareness.

**Skills:**

| Skill | What it handles |
|---|---|
| `helm-operation` | Install, upgrade, rollback, uninstall, search charts, validate values, dry-run, and plan generation |

**What you can ask it to do:**

- "Search for MySQL charts on Bitnami"
- "Install nginx from bitnami in the web namespace"
- "Upgrade the API release to version 3.0"
- "Rollback the web release to the previous version"
- "Show me all Helm releases in staging"
- "What values does the Redis chart support?"
- "Uninstall the test-app release from dev"

**Safety rules:**
- Always runs a dry-run before new installations
- Checks if a release already exists before installing (auto-switches to upgrade if needed)
- Preserves existing configuration on upgrades (`--reuse-values` by default)
- Validates chart values against the schema before applying
- Uses real revision numbers from release history for rollbacks — never guesses

**What it won't do:** Anything outside Helm. If you ask it to sync an ArgoCD app or configure Traefik middleware, it politely declines and tells you which operator to use instead.

---

### App Operator

**What it does:** Manages application delivery across three integrated systems — ArgoCD for GitOps deployments, Argo Rollouts for progressive delivery (canary, blue-green), and Traefik for edge traffic routing.

**When the Supervisor routes to it:** When you mention deploying applications, syncing GitOps repos, running canary deployments, splitting traffic, configuring middleware, or anything related to ArgoCD, Argo Rollouts, or Traefik.

**MCP Servers:**

| Server | What it connects to |
|---|---|
| `argocd-mcp-server` | ArgoCD — application lifecycle, sync, rollback, project management |
| `argo-rollout-mcp-server` | Argo Rollouts — canary, blue-green, promote, abort, analysis |
| `traefik-mcp-server` | Traefik — routes, traffic splitting, middleware, TLS, mirroring |

**Skills:**

| Skill | What it handles |
|---|---|
| `argocd-gitops` | Application onboarding, sync, rollback, health monitoring, multi-environment promotion |
| `argo-rollout-gitops` | Deployment-to-Rollout migration, canary/blue-green rollouts, AnalysisTemplates, promotion control |
| `traefik-edge-routing` | IngressRoute management, weighted traffic splitting, middleware, TLS, NGINX migration |

**What you can ask it to do:**

- "Onboard my app to ArgoCD" — sets up the project, repository, and application in one go
- "Sync the frontend application"
- "Promote the canary to 50%" — advances a progressive rollout
- "Set up a blue-green deployment for the checkout service"
- "Split traffic 80/20 between v1 and v2"
- "Add rate limiting middleware to the API route"
- "Migrate my NGINX ingress to Traefik"
- "Check if there are any out-of-sync apps"

**Safety rules:**
- Autonomous canary promotion only up to 50% traffic weight — anything beyond requires explicit approval
- Traefik routes are always generated first (`generate` mode), shown to you, and only applied after confirmation
- Never creates TCP routes without approval (no automatic rollback for TCP)
- Traffic mirroring capped at 20% without explicit approval
- Never zeroes out all traffic weights — at least one backend must always receive traffic
- Verifies backend services exist before creating routes to them

**What it won't do:** Raw Kubernetes operations (scaling pods, creating ConfigMaps) or Helm chart management. Those go to the K8s Operator or Helm Operator respectively.

---

### K8s Operator

**What it does:** Handles direct Kubernetes cluster operations — listing resources, debugging failing pods, scaling workloads, creating and updating manifests, managing secrets, inspecting events, and switching between clusters.

**When the Supervisor routes to it:** When you mention pods, deployments, services, ConfigMaps, secrets, scaling, logs, exec, namespaces, cluster health, RBAC, or anything that's a raw Kubernetes operation.

**MCP Server:** `kubernetes-mcp-server` — provides tools for resource CRUD, pod lifecycle (logs, exec, run), workload scaling, cluster events, node diagnostics, and multi-cluster context management.

**Skills:**

| Skill | What it handles |
|---|---|
| `kubernetes-cluster-ops` | Resource lifecycle, pod debugging, workload scaling, cluster events, node diagnostics, RBAC, multi-cluster context |

**What you can ask it to do:**

- "List all pods in the staging namespace"
- "Why is my API pod crashing?" — investigates CrashLoopBackOff with logs, events, and resource analysis
- "Scale the web deployment to 5 replicas"
- "Show me the logs from the checkout pod"
- "Exec into the frontend container"
- "Create a ConfigMap with my app settings"
- "Is my cluster healthy?" — runs a comprehensive multi-tool health assessment
- "What events are happening in the production namespace?"
- "Switch to the staging cluster context"
- "Show me CPU and memory usage across all nodes"

**Safety rules:**
- Production namespaces (`prod`, `production`, `kube-system`) get elevated caution — always confirms before mutations
- Never displays secret values — acknowledges key names but masks data
- Reads before writing — always checks current state before scaling, updating, or creating
- Pod exec requires confirmation of the target pod, namespace, and exact command
- Force-delete only when you explicitly say "force delete"
- Multi-cluster: verifies the active context before any write operation

**What it won't do:** Helm chart operations, ArgoCD syncs, or observability tasks. Those go to their respective operators.

---

### Observability Operator

**What it does:** Manages the full observability stack across five pillars — Prometheus for metrics, Alertmanager for alert lifecycle, OpenTelemetry for instrumentation pipelines, Loki for log exploration, and Tempo for distributed tracing.

**When the Supervisor routes to it:** When you mention metrics, alerts, logs, traces, monitoring, PromQL, LogQL, TraceQL, silences, exporters, collectors, or anything related to observability.

**MCP Servers:**

| Server | What it connects to |
|---|---|
| `prometheus-mcp-server` | Prometheus — PromQL queries, exporters, ServiceMonitors, alerting/recording rules, cardinality |
| `alertmanager-mcp-server` | Alertmanager — alert triage, silences, routing, receiver testing |
| `opentelemetry-mcp-server` | OpenTelemetry — collector provisioning, auto-instrumentation, pipeline validation, sampling |
| `loki-mcp-server` | Loki — LogQL queries, label discovery, log patterns, trace-log correlation |
| `tempo-mcp-server` | Tempo — TraceQL queries, trace summarization, RED metrics, service topology |

**Skills:**

| Skill | What it handles |
|---|---|
| `prometheus` | PromQL queries, exporter onboarding, ServiceMonitors, Probes, alerting/recording rules, cardinality optimization |
| `alertmanager` | Alert triage, silence lifecycle, routing audits, receiver testing, governance compliance |
| `opentelemetry` | Collector provisioning, auto-instrumentation, pipeline validation, sampling strategies, SpanMetrics |
| `loki` | LogQL queries, label discovery, log patterns, field analysis, trace-log correlation (read-only) |
| `tempo` | TraceQL queries, trace search, critical path analysis, RED metrics, service topology, Tempo Operator CRDs |

**What you can ask it to do:**

- "What alerts are firing right now?" — summarizes all active alerts by severity
- "Mute checkout alerts for 2 hours" — creates a targeted silence
- "How much CPU is my service using?" — runs a PromQL query
- "Monitor my endpoint https://api.example.com" — sets up a blackbox exporter and Probe
- "Show logs for checkout errors" — queries Loki with LogQL
- "Find slow checkout requests" — searches Tempo for high-latency traces
- "Onboard my service to OpenTelemetry" — auto-instruments the deployment
- "Why is checkout slow? Check metrics and traces" — cross-pillar investigation across Prometheus and Tempo
- "Who gets paged when CPU is high?" — audits Alertmanager routing
- "Deploy a collector for traces and metrics" — provisions an OTel collector

**Cross-pillar correlation:** This operator can work across multiple observability tools in a single investigation. For example, if you ask "Why is checkout slow?", it might:
1. Check error rate and latency in **Prometheus**
2. Find slow traces in **Tempo**
3. Look up related error logs in **Loki**
4. Check if there are active alerts in **Alertmanager**

**Safety rules:**
- Always previews silence blast radius before creating silences
- Max silence duration: 24 hours
- Test alerts fire real notifications — warns you before pushing test alerts
- OpenTelemetry state changes always run in `dry_run` mode first
- Auto-instrumentation triggers pod restarts — always warns during planning
- Loki is entirely read-only — no state-modifying operations
- PromQL counter queries always use `rate()` or `increase()` — never raw counter values
- Tempo CRD changes default to `dry_run=true`

**What it won't do:** Raw Kubernetes operations, Helm chart management, or application deployment. Those go to their respective operators.

---

## How operators connect to external systems

Each operator talks to the outside world through **MCP servers** (Model Context Protocol). These are lightweight processes that translate the agent's tool calls into API calls to the actual systems.

The connection pattern is **Just-In-Time (JIT)**: an MCP server only starts when its operator needs it, and shuts down immediately after the task completes. This keeps resource usage minimal — even though there are 10 registered MCP servers, only the ones actively being used consume resources.

All MCP servers default to **stdio transport** — each server runs as a subprocess and communicates via stdin/stdout. For Docker Compose deployments, you can switch to HTTP transport. See [MCP Servers](./mcp-servers.md) for transport configuration details.

## How requests move between operators

Sometimes your request starts in one operator but needs help from another. For example, you might ask the Observability Operator about a failing service, and it discovers the underlying pods are in CrashLoopBackOff — which is the K8s Operator's territory.

When this happens, the operator sends a structured handoff back to the Supervisor:

```
"This is outside my scope. Please use the appropriate operator."
User Request: <your original request>
Context: <what the first operator already discovered>
```

The Supervisor picks this up, preserves everything the first operator found, and routes to the correct operator with all the context intact. You don't have to repeat yourself — the second operator sees all the prior findings and can start working immediately.

**Example flow:**

1. You ask: "Why is checkout timing out?"
2. **Observability Operator** checks Prometheus → finds 5xx error rate spike and high latency traces in Tempo.
3. It hands off to K8s Operator: "Pod health investigation needed."
4. **K8s Operator** receives the prior findings (error spike, latency data), checks pod status → finds 2/3 pods in CrashLoopBackOff with OOM errors.
5. You get a unified answer covering both observability data and the underlying pod issue.

## How operators are loaded at startup

Operators are discovered automatically from the `built_in_subagents/` directory when k8s-autopilot starts. Each operator directory follows this structure:

```
built_in_subagents/{operator-name}/
├── .mcp.json          # MCP server connections for this operator
├── agents/
│   └── {operator}.md  # The operator's system prompt, capabilities, and safety rules
└── skills/
    └── {skill-name}/
        ├── SKILL.md           # Step-by-step workflow playbook
        └── references/
            └── workflows.md   # Detailed workflow examples and edge cases
```

- **`.mcp.json`** — defines which MCP servers this operator connects to and their environment variables
- **`agents/*.md`** — the operator's identity, mission, classification rules, decision policies, safety guardrails, and example interactions
- **`skills/*/SKILL.md`** — detailed, step-by-step playbooks that the operator loads when performing complex or state-changing operations

The `SubagentsMiddleware` handles injecting all discovered operators into the Supervisor's tool set at startup.

## At a glance

| Operator | Domain | MCP Servers | Skills | Example request |
|---|---|---|---|---|
| **Helm** | Helm chart lifecycle | helm-mcp-server | helm-operation | "Install nginx from bitnami" |
| **App** | Application delivery | argocd, argo-rollout, traefik | argocd-gitops, argo-rollout-gitops, traefik-edge-routing | "Deploy my app with canary" |
| **K8s** | Kubernetes cluster ops | kubernetes-mcp-server | kubernetes-cluster-ops | "Why is my pod crashing?" |
| **Observability** | Monitoring & tracing | prometheus, alertmanager, opentelemetry, loki, tempo | prometheus, alertmanager, opentelemetry, loki, tempo | "What alerts are firing?" |

## Next steps

- **[Configuration](./configuration.md)** — Settings UI and environment variable reference
- **[MCP Servers](./mcp-servers.md)** — Server reference, transport options, and security classification
- **[HITL Governance](./hitl-governance.md)** — How the approval system protects your infrastructure
