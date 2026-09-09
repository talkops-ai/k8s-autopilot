# MCP servers

> Connect k8s-autopilot to external tools using the Model Context Protocol

k8s-autopilot supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). MCP lets you connect external tool providers — Kubernetes cluster inspectors, CI/CD platforms, monitoring systems, cloud APIs — directly into the agent without writing custom code. Each MCP server exposes a set of tools that the agent can discover and call like native functions.

## Add an MCP server

### From the Settings UI

Go to **Settings → MCP Servers** and click **+ Add Server**. Enter the server name, command, arguments, and environment variables. The server is saved to the configuration database and available immediately — no restart needed.

### From a config file

MCP servers are configured in `.mcp.json` files using the standard format:

```json
{
  "mcpServers": {
    "my-database": {
      "command": "npx",
      "args": ["-y", "@my-org/db-mcp-server"],
      "env": {
        "DATABASE_URL": "postgresql://localhost:5432/mydb"
      }
    },
    "my-cloud": {
      "command": "uvx",
      "args": ["cloud-mcp-server"],
      "env": {
        "API_KEY": "${MY_CLOUD_API_KEY}"
      }
    }
  }
}
```

Each server entry defines:

| Field | Type | What it does |
|---|---|---|
| `command` | string | Executable to launch (`npx`, `python`, `uvx`, `docker`, or a binary name) |
| `args` | list | Arguments passed to the server process |
| `env` | object | Environment variables for the server process |
| `url` | string | URL for HTTP/SSE transport (use instead of `command` for remote servers) |
| `transport` | string | Transport type: `stdio` (default), `http`, or `sse` |
| `headers` | object | HTTP headers for remote transports (e.g. auth tokens) |
| `disabled_tools` | list | Glob patterns for tools to exclude (e.g. `["delete_*", "drop_*"]`) |
| `allowed_tools` | list | Glob patterns for tools to include (only matching tools are loaded) |

### Environment variable expansion

Config values support `${VAR}` and `${VAR:-default}` syntax (POSIX `:-` semantics). The `:-` form falls back to the default when the variable is unset or empty:

```json
{
  "env": {
    "PROMETHEUS_URL": "${PROMETHEUS_BASE_URL:-http://localhost:9090}",
    "API_TOKEN": "${MY_SECRET_TOKEN}"
  }
}
```

If a `${VAR}` reference has no default and the variable is unset, k8s-autopilot rejects the config with a clear error message rather than passing through an empty value.

## Where configs are loaded from

k8s-autopilot discovers MCP server configs from multiple locations and merges them together. The database is the source of truth — file-based configs are auto-seeded into the database on first discovery.

| Priority | Location | Source label |
|---|---|---|
| 1 | **Database** (Settings UI) | `db` |
| 2 | **Global user configs** | `global` |
|   | `~/.agents/.mcp.json` or `~/.agents/mcp.json` | |
|   | `~/.k8s_autopilot/.mcp.json` or `~/.k8s_autopilot/mcp.json` | |
| 3 | **Project-level configs** | `project` |
|   | `.mcp.json`, `mcp.json`, `.k8s_autopilot/.mcp.json` in project root | |
| 4 | **Plugin configs** | `plugin:{name}` |
|   | `plugins/{name}/.mcp.json` for non-agent plugins | |
| 5 | **Installed plugin configs** | `plugin:{name}` |
|   | Plugins installed from the marketplace that bundle MCP servers | |

Operator-bundled MCP servers (in `built_in_subagents/`) are **not** loaded globally. They are isolated — each operator connects to its own MCP servers only when invoked. See [Operators and Sub-agents](./operators-and-subagents.md) for details.

## How tools are named

When k8s-autopilot connects to an MCP server, it discovers all available tools and registers them with a namespaced name:

```
mcp__{server}__{tool}
```

For example, a tool `get_pods` from server `kubernetes` becomes `mcp__kubernetes__get_pods`. This prevents name collisions when multiple servers expose tools with the same name.

## Transports

### stdio (default)

The MCP server runs as a subprocess of k8s-autopilot, communicating via stdin/stdout. No network configuration needed. This is the simplest setup — just specify `command` and `args`.

```json
{
  "command": "npx",
  "args": ["-y", "kubernetes-mcp-server@latest"]
}
```

### HTTP

For Docker Compose, remote deployments, or shared MCP servers, use HTTP transport:

```json
{
  "url": "http://my-mcp-server:8080",
  "transport": "http",
  "headers": {
    "Authorization": "Bearer ${MCP_AUTH_TOKEN}"
  }
}
```

You can also override built-in operator servers to HTTP transport using the `MCP_SERVERS` environment variable (a JSON array):

```bash
MCP_SERVERS='[
  {"name": "helm_mcp_server", "url": "http://helm-mcp:8080", "transport": "http"},
  {"name": "prometheus-mcp-server", "url": "http://prom-mcp:8080", "transport": "http"}
]'
```

### SSE (Server-Sent Events)

For servers that use the SSE streaming protocol:

```json
{
  "url": "http://my-server:8080/sse",
  "transport": "sse"
}
```

If the transport is not specified, k8s-autopilot auto-detects it: if `url` contains "sse" it uses SSE, if `url` is present it uses HTTP, otherwise it defaults to stdio.

## Trust and security

### Trust levels

Not all MCP configs are equally trusted. Configs committed to a shared Git repository could be contributed by anyone, so k8s-autopilot uses SHA-256 fingerprinting to gate project-level configs:

- **Global configs** (`~/.agents/`, `~/.k8s_autopilot/`) — always trusted. You control these.
- **Database configs** (Settings UI) — always trusted. You added them yourself.
- **Built-in operator configs** — always trusted. They ship with k8s-autopilot.
- **Project-level configs** (`.mcp.json` in project root) — require explicit approval on first use. k8s-autopilot remembers your trust decisions by storing the file's SHA-256 fingerprint. If the file changes, it asks again.

### Semantic tool profiling

When an MCP server connects, k8s-autopilot automatically analyzes every tool it exposes and classifies it into a security tier. This happens at registration time — no runtime overhead:

| Tier | Classification | Behavior | Examples |
|---|---|---|---|
| **Tier 1** — Read-only | Strictly idempotent inspection | Runs automatically | `get_pods`, `query_metrics`, `list_releases`, `search_charts` |
| **Tier 2** — Low-impact | Reversible or transient mutations | Runs with light confirmation | `dry_run_install`, `add_label`, `annotate` |
| **Tier 3** — Mutating | Modifies live infrastructure | Gated by HITL approval | `install_chart`, `scale_deployment`, `apply_manifest`, `sync_app` |
| **Tier 4** — Destructive | Irreversible deletion or eviction | Blocked without explicit allowlist | `delete_namespace`, `uninstall_release`, `drain_node`, `drop_database` |

The profiler uses both **MCP tool annotations** (the standard `readOnlyHint` and `destructiveHint` fields) and **heuristic verb analysis** (scanning tool names for patterns like `get_`, `list_`, `delete_`, `uninstall_`). Annotations always take precedence over heuristics.

### Headless mode safety

When k8s-autopilot runs without a UI (headless mode, CI/CD, Slack integration), the `HeadlessMCPGuardMiddleware` automatically blocks all Tier 3+ MCP tool calls that don't have a coherent `readOnlyHint=true` annotation. This prevents unattended mutations.

### Tool filtering

You can restrict which tools are available from any server using glob patterns:

```json
{
  "mcpServers": {
    "kubernetes": {
      "command": "npx",
      "args": ["-y", "kubernetes-mcp-server@latest"],
      "allowed_tools": ["get_*", "list_*", "describe_*"],
      "disabled_tools": ["delete_*", "exec_*"]
    }
  }
}
```

- `allowed_tools` — only matching tools are loaded (whitelist)
- `disabled_tools` — matching tools are excluded (blacklist)
- If both are specified, `disabled_tools` is checked first

## JIT connection lifecycle

MCP connections follow a Just-In-Time pattern for operator-bundled servers:

1. You ask a question that gets routed to an operator
2. The operator's `CompiledSubAgent` wrapper opens the MCP connection
3. The operator executes its tools via MCP
4. The connection closes immediately after the operator completes

This prevents idle connections when many servers are registered but only a subset is used per conversation. Global (non-operator) MCP servers stay connected for the duration of the session.

## MCP timeout settings

| Variable | Default | What it controls |
|---|---|---|
| `MCP_TIMEOUT` | `30` | Per-tool execution timeout in seconds |
| `MCP_TIMEOUT_TOTAL` | `600` | Total operation timeout in seconds |
| `MCP_TIMEOUT_CONNECT` | `300` | Server connection / startup timeout in seconds |
| `MCP_DEFAULT_TRANSPORT` | `sse` | Default transport for new servers: `sse`, `stdio`, or `http` |

## Enabling and disabling servers

You can enable or disable any MCP server from the **Settings → MCP Servers** panel in the UI. Disabled servers are not connected during startup and their tools are not available to the agent.

From the config file, use the `enabled` field:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "my-mcp-server",
      "enabled": false
    }
  }
}
```

## Next steps

- **[Configuration](./configuration.md)** — Full settings reference including MCP environment variables
- **[Operators and Sub-agents](./operators-and-subagents.md)** — Which operators use which MCP servers
- **[HITL Governance](./hitl-governance.md)** — How the approval system works with MCP tool tiers
