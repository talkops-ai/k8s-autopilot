# k8s-autopilot

> An AI assistant that manages your Kubernetes clusters — it can deploy apps, monitor health, troubleshoot issues, and handle day-to-day operations, all through a chat interface

k8s-autopilot is an open-source AI operations framework built on the [Deep Agents SDK](https://docs.langchain.com/oss/python/deepagents/quickstart) and [LangGraph](https://docs.langchain.com/langgraph). You talk to it in plain English, and it connects to your cluster to get things done — deploying Helm charts, managing ArgoCD applications, querying Prometheus metrics, reading logs, and more.

It works with 20+ AI model providers (OpenAI, Anthropic, Google, and others), ships with 4 built-in domain operators, and can be extended with plugins from a marketplace — all configurable from a web UI.

Unlike general-purpose AI assistants, k8s-autopilot understands infrastructure. It knows about Helm release state, ArgoCD sync drift, canary deployments, alert correlation, and why deleting a production namespace without review is a terrible idea. It always shows you a plan and asks for approval before making changes — nothing happens to your cluster without your say-so.

## Quick install

```bash
curl -LsSf https://raw.githubusercontent.com/talkops-ai/k8s-autopilot/main/scripts/install.sh | bash
```

Open **http://localhost:8888** to access the web UI and configure everything from Settings.

See the [Quickstart](./quickstart.md) to configure your AI provider credentials and run your first task.

## What can it do?

### Built-in tools

The central agent has a small set of general-purpose tools it can use directly:

| Tool | What it does |
|---|---|
| `web_search` | Searches the web for documentation, error codes, CVEs, and release notes |
| `fetch_url` | Reads a webpage and extracts the useful content |
| `get_goal` / `update_goal` | Tracks progress toward a goal you've set |
| `get_rubric` | Retrieves quality criteria for checking the agent's own work |

All Kubernetes-specific work (Helm installs, ArgoCD syncs, kubectl commands, Prometheus queries) is delegated to specialized sub-agents that connect to your cluster through dedicated tool servers — the central agent orchestrates, the sub-agents execute.

### Built-in operators

k8s-autopilot ships with 4 domain operators. Think of each operator as a specialist focused on one area of Kubernetes operations:

| Operator | What it handles | How it works |
|---|---|---|
| **🎡 Helm Operator** | Installing, upgrading, and rolling back Helm charts. Can also generate new charts and push them to GitHub. | Connects to the Helm MCP server (22 tools). Internally runs a multi-phase pipeline — planning, generating, validating, executing, and committing. |
| **📦 App Operator** | Managing applications with ArgoCD (GitOps), Argo Rollouts (canary and blue-green deployments), and Traefik (traffic routing). | Connects to ArgoCD, Argo Rollouts, and Traefik MCP servers. |
| **☸️ K8s Operator** | Core cluster tasks — listing resources, debugging crashing pods, checking permissions (RBAC), scaling workloads, switching between clusters. | Connects to the Kubernetes MCP server for direct cluster access. |
| **📊 Observability Operator** | Monitoring and troubleshooting — querying Prometheus metrics, triaging alerts, searching logs (Loki), tracing requests (Tempo), and managing OpenTelemetry pipelines. | Connects to 5 dedicated MCP servers — one per monitoring system. |

Each operator works independently — its intermediate steps (query iterations, plan drafts, API calls) stay contained and don't clutter your main conversation. See [Operators and Sub-agents](./operators-and-subagents.md) for the full reference.

### How the agent keeps you safe

You control how much freedom the agent has. There are three approval modes, and you can switch between them anytime from the UI:

| Mode | How it works |
|---|---|
| **🛡️ Manual** | The agent pauses and asks before every action that could change something on your cluster. Nothing happens without your approval. |
| **🤖 Auto** | Read-only actions (listing pods, querying metrics) run immediately. Anything that could modify state (deploying, scaling, deleting) still requires your approval. |
| **⚡ YOLO** | Everything runs without asking. Designed for development and testing environments where speed matters more than review. |

### Goals and quality checks

You can give the agent a goal (like "deploy nginx to the staging namespace and make sure it's healthy"), and it will:

1. **Generate acceptance criteria** — a checklist of specific, verifiable conditions
2. **Work toward the goal** — executing the necessary steps
3. **Check its own work** — a separate grader evaluates the results against the criteria
4. **Fix and retry** — if anything fails, it iterates until every criterion passes

This works both interactively (you watch and guide) and autonomously (rubric mode for CI/CD pipelines). See [Goals & Rubrics](./goals-and-rubrics.md).

### Extending with plugins

k8s-autopilot is designed to grow with your needs. From the UI, you can:

- **Install plugins** from a marketplace — each plugin can add new skills (playbooks), sub-agents (new specialists), and MCP servers (tool connections)
- **Connect custom tool servers** — any system that speaks the [Model Context Protocol](https://modelcontextprotocol.io/) can be plugged in directly
- **Use existing plugins** from other ecosystems — plugins built for Anthropic (Claude), OpenAI (Codex), or OpsCode work out of the box

Everything is configurable at runtime — no config files to edit, no containers to restart. See [Plugins](./plugins.md).

### What else is built in

| Feature | What it does |
|---|---|
| **20+ AI providers** | OpenAI, Anthropic, Google, Azure, AWS Bedrock, Groq, DeepSeek, Ollama, and many more — switchable from the UI at any time |
| **11 built-in tool servers** | Helm, ArgoCD, Argo Rollouts, Traefik, Kubernetes, Prometheus, Alertmanager, Loki, Tempo, OpenTelemetry, GitHub |
| **Persistent memory** | The agent remembers important operational context (release names, namespaces, chart URLs) even when older conversation messages are summarized |
| **Cost tracking** | Real-time token usage and cost in USD across all AI providers |
| **Context management** | Automatically summarizes older messages when the conversation gets long, so the agent doesn't lose track |
| **Interactive UI cards** | Approval cards with action details, observability dashboards with charts, and structured input forms — all rendered inline in the conversation |
| **Hooks** | Run your own custom logic before or after any tool execution |
| **UI-driven configuration** | API keys, model selection, cluster connectivity, database backend, tool servers, plugins — all configurable from Settings without restarts |

## Architecture

k8s-autopilot is built on:

- **[Deep Agents SDK](https://docs.langchain.com/oss/python/deepagents/quickstart)** — The agent framework that handles the middleware pipeline, sub-agent orchestration, and response streaming
- **[LangGraph](https://docs.langchain.com/langgraph)** — Manages conversation state and checkpoints it to SQLite or PostgreSQL so sessions survive restarts
- **[LangChain](https://docs.langchain.com/)** — Provides a unified interface to talk to 20+ AI model providers
- **[MCP](https://modelcontextprotocol.io/)** — The Model Context Protocol that connects the agent to 11 built-in infrastructure tool servers
- **[A2A SDK](https://a2a-protocol.org/latest/)** — Agent-to-Agent protocol used for inter-agent communication and the web UI
- **[Starlette](https://www.starlette.io/)** — The web server framework powering the HTTP API

### How a request flows

When you type something in the chat, here's what happens:

1. **You ask** — "deploy nginx with Helm" or "why is my pod crashing?"
2. **The supervisor routes** — it reads your intent and hands the task to the right operator
3. **The operator plans** — it inspects current cluster state and builds a step-by-step plan
4. **You approve** — the plan is shown as an interactive card; nothing runs until you say yes
5. **Sub-agents execute** — the approved plan is executed through MCP tool servers
6. **Results are verified** — if you've set a goal, the grader checks results and iterates until everything passes

Read-only operations (listing pods, querying metrics, checking status) skip the approval step and run immediately.

### Security layers

Multiple security checks run on every operation, regardless of which approval mode you're using:

| Layer | What it protects against |
|---|---|
| **Shell safety scanner** | Classifies every command as safe (read-only) or unsafe (can change things) before it runs |
| **Command parser** | Analyzes complex shell commands (pipes, chains, redirections) to catch hidden dangerous patterns |
| **Text scanner** | Detects invisible Unicode tricks that could make commands look different from what they actually do |
| **URL validator** | Blocks requests to internal network addresses and cloud metadata endpoints (prevents SSRF attacks) |
| **Headless guard** | When running unattended, automatically blocks destructive and privileged operations |

### Where data is stored

| Location | What's in it |
|---|---|
| `~/.k8s-autopilot/` | Installation root — Docker Compose files and configuration |
| `~/.k8s-autopilot/.env` | Your API keys and secrets |
| Settings database | Runtime configuration — model settings, tool servers, approval mode, plugins, integration endpoints |
| Conversation database | Chat history and session state (SQLite by default, can switch to PostgreSQL from the UI) |

## Next steps

- **[Quickstart](./quickstart.md)** — Install k8s-autopilot, open the UI, and run your first task.
- **[Configuration](./configuration.md)** — Environment variables, AI model tiers, and how settings are resolved.
- **[Operators and Sub-agents](./operators-and-subagents.md)** — The 4 domain operators and how work gets routed between them.
