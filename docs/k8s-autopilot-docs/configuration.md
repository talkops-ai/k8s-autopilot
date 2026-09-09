# Configuration

> How to configure k8s-autopilot — from the UI, environment variables, or both

k8s-autopilot can be configured in two ways: through the **Settings UI** (recommended) or through **environment variables** in a `.env` file. UI changes take effect immediately — no restart needed. Environment variables are loaded at startup and serve as initial defaults.

## How settings are resolved

When the agent needs a configuration value, it checks three places in order:

1. **Settings UI** (highest priority) — values you've set from the Settings page, stored in the config database
2. **Environment variable** — from your `.env` file or shell exports
3. **Built-in default** — hardcoded defaults in the application

This means you can set everything from the UI and never touch a config file. Or you can set initial values in `.env` and override individual settings later from the UI.

---

## Settings UI walkthrough

Open the Settings page from the web UI by clicking **Setup** on the agent card, or the gear icon in the chat interface. The sidebar has six panels:

### Auth & Keys

This is where you manage API keys for all supported AI providers. Each row shows the provider name, the environment variable it maps to, and its current status.

![Auth & Keys — manage API keys for all AI providers](../assets/configuration/01-auth-keys.jpg)

Click on any provider row to open its key management dialog:

![API key dialog — paste your key, view status, and access advanced settings](../assets/configuration/02-api-key-modal.jpg)

In the dialog you can:

- **See the status** — a green "Key is configured and active" badge confirms the key is stored and working
- **Paste a new key** — just paste into the field and click **Save Key**. The key is stored securely in the server's config store and redacted over API responses.
- **Access Advanced Settings** — expand to configure custom base URLs for self-hosted or proxied providers
- **Remove a key** — click **Remove Key** to delete the credential

Keys take effect immediately — the model becomes available in the chat interface without restarting.

> [!TIP]
> You only need **one** provider key to get started. You can add more providers at any time to unlock additional models.

### Plugins

Discover, install, and manage plugins that extend the agent's capabilities. The Plugins panel gives you access to the marketplace where you can browse available plugins from the community and your organization.

![Plugins & Extensions — discover and install plugins from the marketplace](../assets/configuration/03-plugins.jpg)

The plugin page has four tabs:

- **Discover** — Browse available plugins from all configured marketplaces. Each plugin card shows its name, version, skill count, description, and publisher. Click **Install** to add it.
- **Installed** — View and manage plugins you've already installed. Enable, disable, or uninstall plugins here.
- **Marketplaces** — Add or remove marketplace URLs. You can add your organization's private marketplace alongside the public one.
- **Errors** — View any plugin installation or loading errors.

Each plugin can add skills (operational playbooks), sub-agents, and MCP server connections. Use **+ Add Marketplace** to register a private registry, or **+ Custom Plugin** to load a plugin from a local path or URL.

See [Plugins](./plugins.md) for the full plugin reference.

### Traces

Configure LangSmith tracing for full observability into agent execution — every LLM call, tool invocation, prompt, and token count gets exported.

![Traces — configure LangSmith tracing for execution monitoring](../assets/configuration/04-traces.jpg)

- **LangSmith Tracing** toggle — flip this on to start exporting traces
- **API Key** (`LANGSMITH_API_KEY`) — your LangSmith API key, stored securely on the server
- **Project Name** (`LANGSMITH_PROJECT`) — which LangSmith project receives the traces (default: `k8s-autopilot`)
- **Endpoint URL** (`LANGSMITH_ENDPOINT`) — the LangSmith API endpoint (default: `https://api.smith.langchain.com`)

### Runtime Config

The most feature-rich panel. This is where you configure backend storage, cluster connectivity, approval governance, integration endpoints, and custom environment variables.

#### Backend Storage

Choose where the agent stores its checkpoints, conversation state, and configuration:

![Backend Storage — choose between SQLite and PostgreSQL](../assets/configuration/05-runtime-storage.jpg)

- **SQLite** (default) — local file persistence, zero-configuration, perfect for single instances and development
- **PostgreSQL** — production multi-replica persistent storage for team and enterprise deployments

The **Active SQLite Database** field shows the current database file. Click **⚡ Test Connection** to verify connectivity before switching backends. You can hot-swap between SQLite and PostgreSQL at runtime.

| Variable | Default | What it sets |
|---|---|---|
| `CHECKPOINT_BACKEND` | `auto` | Storage backend: `sqlite`, `postgres`, or `auto` (auto-detects from `POSTGRES_URI`) |
| `SQLITE_FILE` | `k8s_autopilot.db` | SQLite database filename |
| `POSTGRES_URI` | — | PostgreSQL connection string (e.g. `postgresql://user:pass@host:5432/db`) |

#### Kubernetes Cluster Connectivity

Configure how the agent connects to your Kubernetes cluster. These settings are used by the Kubernetes, Helm, and Application operators.

![Cluster Connectivity — kubeconfig, context, namespace, and sandbox provider](../assets/configuration/06-runtime-cluster.jpg)

- **Kubeconfig Path** (`KUBECONFIG`) — path to your kubeconfig file (default: `~/.kube/config`)
- **Cluster Context** (`KUBE_CONTEXT`) — which context to use from your kubeconfig
- **Default Namespace** (`KUBE_NAMESPACE`) — the default namespace for operations (default: `default`)
- **Sandbox Provider** (`SANDBOX_PROVIDER`) — where to execute sandbox commands: `local` (host execution), `docker`, or `kubernetes`

#### Application & Routing

Configure ArgoCD and Traefik integration endpoints for the Application operator:

| Variable | Default | What it sets |
|---|---|---|
| `ARGOCD_SERVER_URL` | — | ArgoCD API server URL |
| `ARGOCD_AUTH_TOKEN` | — | ArgoCD authentication token |
| `ARGOCD_INSECURE` | `true` | Skip TLS verification for ArgoCD |
| `TRAEFIK_API_URL` | — | Traefik API / dashboard URL |

#### Observability

Configure the monitoring stack endpoints used by the Observability operator:

| Variable | Default | What it sets |
|---|---|---|
| `PROMETHEUS_BASE_URL` | `http://prometheus-operated.monitoring.svc:9090` | Prometheus server URL |
| `ALERTMANAGER_BASE_URL` | `http://alertmanager-operated.monitoring.svc:9093` | Alertmanager server URL |
| `LOKI_URL` | `http://localhost:3100` | Loki log aggregation URL |
| `TEMPO_BASE_URL` | `http://localhost:3200` | Tempo distributed tracing URL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OpenTelemetry OTLP Collector endpoint |

#### Environment & Custom Configuration

At the bottom of the Runtime Config page, you can inject arbitrary key-value pairs as environment variables into the agent's runtime. This is useful for setting custom variables that your plugins, skills, or MCP servers need — without modifying the `.env` file or restarting the stack.

---

## AI model configuration

k8s-autopilot uses a **unified model system** — one active model at a time. You can switch models and providers at any time from the UI or via environment variables.

| Variable | Default | What it sets |
|---|---|---|
| `MODEL` | `gemini-3.7-flash` | The active model name (e.g. `gpt-4o`, `claude-sonnet-4-20250514`, `gemini-3.7-flash`) |
| `MODEL_PROVIDER` | (auto-detected) | The provider to use. If not set, the agent detects it from the model name and available API keys. |
| `REASONING_EFFORT` | `medium` | How much "thinking" the model does. Options: `low`, `medium`, `high`, `max`. |
| `MODEL_CONTEXT_LIMIT` | (model default) | Override the model's context window token limit. |

### Switching models

**From the UI:** The chat interface includes a model picker. Select any model from any configured provider — the switch takes effect immediately.

**From `.env`:**

```bash
# Google Gemini
MODEL=gemini-3.7-flash
MODEL_PROVIDER=google_genai
GOOGLE_API_KEY=your_key

# Or OpenAI
MODEL=gpt-4o
MODEL_PROVIDER=openai
OPENAI_API_KEY=your_key

# Or Anthropic
MODEL=claude-sonnet-4-20250514
MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_key
```

### Supported providers

`openai`, `anthropic`, `google_genai`, `google_vertexai`, `azure_openai`, `bedrock`, `groq`, `deepseek`, `together`, `fireworks`, `openrouter`, `mistralai`, `nvidia`, `perplexity`, `cohere`, `ibm`, `huggingface`, `litellm`, `xai`, `baseten`, `ollama`

See [Model Providers](./model-providers.md) for the full reference with auth details.

### Provider base URLs

For self-hosted or proxied providers, you can override the API base URL:

| Variable | Provider |
|---|---|
| `OPENAI_BASE_URL` | OpenAI |
| `ANTHROPIC_BASE_URL` | Anthropic |
| `GEMINI_API_BASE` | Google Gemini |
| `OPENROUTER_API_BASE` | OpenRouter |
| `GROQ_BASE_URL` | Groq |
| `DEEPSEEK_API_BASE` | DeepSeek |

### Google Vertex AI

To use Vertex AI instead of Google AI Studio:

```bash
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
```

### Reasoning effort

Controls how much reasoning/thinking the model performs. Higher values produce more thorough results but use more tokens:

```bash
REASONING_EFFORT=high    # Options: low, medium, high, max
```

---

## MCP server configuration

MCP servers default to **stdio transport** — each server runs as a subprocess of k8s-autopilot, communicating via stdin/stdout. No network configuration needed.

For Docker Compose or remote deployments, you can override to **HTTP transport** using the `MCP_SERVERS` environment variable (a JSON array):

```bash
MCP_SERVERS='[
  {"name": "helm_mcp_server", "url": "http://helm-svc:8080", "transport": "http"},
  ...
]'
```

### MCP timeout settings

| Variable | Default | What it sets |
|---|---|---|
| `MCP_TIMEOUT` | `30` | Per-tool execution timeout (seconds) |
| `MCP_TIMEOUT_TOTAL` | `600` | Total operation timeout (seconds) |
| `MCP_TIMEOUT_CONNECT` | `300` | Server connection timeout (seconds) |
| `MCP_DEFAULT_TRANSPORT` | `sse` | Default transport for new servers: `sse`, `stdio`, or `http` |

See [MCP Servers](./mcp-servers.md) for the full server reference and transport configuration.

---

## Environment variable aliases

k8s-autopilot automatically syncs environment variable aliases so different MCP servers can find the same configuration:

- `PROMETHEUS_BASE_URL` ↔ `PROMETHEUS_URL` (used by argo-rollout-mcp-server)
- `KUBECONFIG` ↔ `K8S_KUBECONFIG` (used by argo-rollout and traefik MCP servers)
- `GOOGLE_API_KEY` ↔ `GEMINI_API_KEY`
- `LANGCHAIN_API_KEY` ↔ `LANGSMITH_API_KEY`

## Other environment variables

| Variable | Default | What it sets |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `LOG_MODE` | `text` | Console output format (`text` or `json`) |
| `HELM_WORKSPACE` | `./workspace/helm-charts` | Directory for generated Helm charts |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | — | GitHub PAT for committing charts |
| `TAVILY_API_KEY` | — | API key for web search |
| `RECURSION_LIMIT` | `50` | LangGraph graph step recursion limit |
| `COMPACTION_TOKEN_BUDGET` | `100000` | Token threshold triggering context compaction |
| `APPROVAL_MODE` | `manual` | Tool execution approval mode: `manual`, `auto`, `yolo` |

## Next steps

- **[Model Providers](./model-providers.md)** — Full provider reference with auth details and custom base URLs.
- **[MCP Servers](./mcp-servers.md)** — Server reference, transport options, and security classification.
- **[Quickstart](./quickstart.md)** — Installation and first-time setup walkthrough.
- **[HITL Governance](./hitl-governance.md)** — How the approval system works.
