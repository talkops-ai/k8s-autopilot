# HITL governance

> How k8s-autopilot keeps your infrastructure safe by asking before acting

k8s-autopilot follows a simple rule: **look before you touch**. Before doing anything that could change your infrastructure — deploying software, scaling workloads, deleting resources — it stops and asks for your permission first.

Think of it like a co-pilot in an aircraft. They can read instruments, check gauges, and monitor systems all day long without asking the pilot. But before touching the controls — throttle, flaps, landing gear — they always call it out and wait for confirmation.

## What needs approval and what doesn't

Not everything requires your permission. k8s-autopilot divides all actions into two buckets:

**Runs automatically (no approval needed):**
- Listing pods, deployments, services, or any other resources
- Checking Helm release status or searching for charts
- Querying Prometheus metrics or searching logs in Loki
- Reading files, searching code, browsing directories
- Any operation that only *reads* information without changing anything

**Pauses and asks you first:**
- Installing, upgrading, or removing Helm charts
- Creating, updating, or deleting Kubernetes resources
- Scaling deployments up or down
- Syncing ArgoCD applications or promoting canary deployments
- Running shell commands that modify state
- Creating alert silences or provisioning collectors
- Pushing code to GitHub

## Three levels of oversight

You get to choose how much control you want. k8s-autopilot offers three approval modes that you can switch between at any time from the Settings UI:

### Manual mode (the default)

Every action that could change your infrastructure pauses and shows you an approval card in the UI. You see exactly what's about to happen — the action, the target resource, the namespace, and what could be affected — before anything runs. You click **Approve** to proceed or **Reject** to stop it.

This is the safest option. Use it when working with production systems, shared clusters, or any environment where a mistake would be costly.

If you reject a plan, the agent doesn't stubbornly retry on its own. It asks you what you'd like to change. After two rejected attempts, it steps back and asks you to rephrase what you're trying to do.

### Auto mode

Auto mode lets the agent handle the obvious stuff on its own while still stopping for anything risky. It works like a smart filter with three layers:

1. **Instant decisions** — The agent already knows that "list pods" is safe and "delete namespace" is dangerous. These are decided instantly without any AI involved. Safe commands run automatically; dangerous ones are blocked.

2. **AI judgment** — For commands in the grey area (like "apply this deployment manifest"), the agent's AI model evaluates whether it's safe enough to proceed. If the AI says no, it asks you.

3. **Ask the human** — When the AI can't decide, or when it's been wrong too many times in a row, it falls back to asking you directly.

What Auto mode considers **always safe** (runs without asking):
- Read-only commands: `kubectl get`, `kubectl describe`, `kubectl logs`, `helm list`, `helm status`, `git status`, `cat`, `ls`, `grep`
- Any MCP tool that's been tagged as read-only by its developer

What Auto mode considers **always dangerous** (always asks):
- Bulk deletions: `kubectl delete --all`, `kubectl delete namespace`
- Operations on production namespaces: `prod`, `production`, `kube-system`
- Node-level operations: `kubectl drain`, `kubectl cordon`
- System-level commands: `rm -rf /`, disk formatting

> [!WARNING]
> Auto mode is a smart safety filter — not a security sandbox. It does a great job catching risky operations, but it's not a replacement for proper access controls on your cluster.

### YOLO mode

Everything runs without stopping to ask. The first time you activate YOLO mode, k8s-autopilot shows a warning explaining the risks and asks you to explicitly acknowledge them.

> [!CAUTION]
> YOLO mode lets the agent run destructive commands without your review. Only use it in throwaway development environments where losing data isn't a concern.

## Asking you questions

Beyond approvals, the agent can also ask you questions during a conversation. This is different from approval gates — it's the agent proactively seeking your input when it needs information it can't figure out on its own.

For example:
- "Which namespace should I deploy this to — staging or production?"
- "I found three matching services. Which one did you mean?"
- "Do you want me to use the existing values or override with these new ones?"

Questions appear as interactive cards in the UI. You can type a free-form answer or pick from a list of choices the agent suggests. The agent waits for your response before continuing.

## How shell commands are checked

When the agent wants to run a shell command, it goes through multiple safety checks before execution:

**Step 1 — Pattern check.** Is this a known-safe command (like `kubectl get pods`) or a known-dangerous one (like `kubectl delete namespace production`)? Known-safe commands pass through; known-dangerous ones are flagged immediately.

**Step 2 — Deep analysis.** For complex commands with pipes (`|`), chains (`&&`), or subshells, the agent parses the full command structure to catch dangerous operations hiding inside what looks like a harmless pipeline. For example, `echo "hello" && kubectl delete namespace prod` would be caught even though it starts with an innocent `echo`.

**Step 3 — Injection detection.** Commands containing shell tricks — backtick execution, variable expansion, redirections, process substitution — are flagged as potentially unsafe regardless of what the command itself does.

**Step 4 — Allowlist check.** If you've configured a list of pre-approved command prefixes (like `kubectl`, `helm`, `terraform`), commands matching those prefixes can bypass interactive prompts. But even allowlisted commands are blocked if they contain injection patterns.

## How MCP tools are classified

Every tool from every connected MCP server gets automatically classified when it connects. k8s-autopilot looks at the tool's name, its description, and any safety annotations the developer included, then assigns it to one of four safety tiers:

| Tier | What it means | What happens |
|---|---|---|
| **Read-only** | Only reads data, never changes anything | Runs automatically in all modes |
| **Low-impact** | Makes small, reversible changes | Auto mode lets the AI decide; Manual mode asks you |
| **Mutating** | Changes live infrastructure | Always asks you (except in YOLO mode) |
| **Destructive** | Deletes things permanently | Always asks you (except in YOLO mode) |

When k8s-autopilot runs without a UI — for example, through a Slack integration or CI/CD pipeline — it's extra cautious. In **headless mode**, anything above read-only is blocked entirely. No mutations happen without a human in the loop.

## How operators add their own safety rules

On top of the framework-level checks described above, each operator adds its own domain-specific safety rules. These are tailored to the risks of each domain:

**Helm Operator** — Asks before installing, upgrading, rolling back, or removing any chart. Always does a dry-run first.

**App Operator** — Asks before syncing or deleting ArgoCD apps. Limits autonomous canary promotion to 50% traffic — anything higher requires your approval. Never creates TCP routes without permission.

**K8s Operator** — Asks before creating, updating, or deleting any resource. Gets extra cautious with production namespaces, never shows secret values, and requires confirmation for pod exec commands.

**Observability Operator** — Asks before creating alert silences, upserting Prometheus rules, provisioning collectors, or modifying Tempo CRDs. Always previews the blast radius of silences before creating them.

### The double-gate pattern

When an operator wants to do something, the approval process has two independent layers:

1. **The operator asks you** — It builds a plan showing what it wants to do and waits for your approval.
2. **The framework checks again** — Even after you approve the plan, the underlying middleware independently verifies the actual MCP tool call before it runs.

This means a tool call has to pass both the operator's domain-specific check *and* the framework's general safety check. Neither layer can bypass the other.

## Built-in protections (always active)

These protections run in the background regardless of which approval mode you're using — even in YOLO mode:

### Invisible character detection

Before any command runs, k8s-autopilot scans for hidden Unicode characters that could be used to trick you. This includes zero-width spaces (invisible characters that change how text is parsed), look-alike characters from other alphabets (a Cyrillic "а" looks identical to a Latin "a" but is a different character), and right-to-left text overrides that can make text appear differently than it actually reads.

### Network safety

When the agent makes web requests, it validates every URL to prevent Server-Side Request Forgery (SSRF) attacks. It blocks requests to internal network addresses, cloud metadata endpoints (which could leak credentials), and reserved IP ranges. DNS is resolved and locked before the request is sent, preventing attackers from swapping addresses mid-request.

### Tool isolation

Each operator can only use its own tools. The Helm operator can call Helm tools but can't access Kubernetes tools. The Observability operator can query Prometheus but can't delete pods. No operator can escalate its own permissions or access tools belonging to another operator.

## Next steps

- **[MCP Servers](./mcp-servers.md)** — How tools are classified and connected
- **[Operators and Sub-agents](./operators-and-subagents.md)** — What each operator does and its safety rules
- **[Configuration](./configuration.md)** — Settings UI and runtime configuration
