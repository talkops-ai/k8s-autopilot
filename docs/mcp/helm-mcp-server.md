# Helm MCP Server

A comprehensive **Model Context Protocol (MCP)** server for managing Kubernetes workloads via Helm. Designed for AI assistants to perform secure, production-grade Helm operations with full validation, monitoring, and best practices guidance.

---

## ✨ Features

### 🔍 Discovery & Search
- Search Helm charts across repositories (Bitnami, ArtifactHub, custom repos)
- Get detailed chart metadata, versions, and documentation
- Access chart READMEs and values schemas

### 🚀 Installation & Lifecycle Management
- Install, upgrade, rollback, and uninstall Helm releases
- Dry-run installations to preview changes before deployment
- Support for custom values, multiple values files, and extra CLI arguments

### ✅ Validation & Safety
- Validate chart values against JSON schemas
- Render and validate Kubernetes manifests before deployment
- Check chart dependencies and cluster prerequisites
- Generate installation plans with resource estimates

### 📊 Monitoring & Status
- Monitor deployment health asynchronously
- Get real-time release status and history
- List all releases across namespaces

### 🔧 Multi-Cluster Support
- List and switch between Kubernetes contexts
- Switch between clusters via kubeconfig context
- Namespace-scoped operations for isolation

### 📚 Built-in Guidance
- Comprehensive workflow guides and best practices
- Security checklists and troubleshooting guides
- Step-by-step procedures for upgrades and rollbacks

---

## ⚙️ Configuration

The Helm MCP Server can be configured using environment variables. All configuration options have sensible defaults, but you can override them to match your environment.

**Note**: When running in Docker, you can override any environment variable using the `-e` flag with `docker run`. The Docker image includes an entrypoint script that sets default values, but any environment variables you provide via `docker run -e` will take precedence over the defaults.

### Environment Variables

#### Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_SERVER_NAME` | `helm-mcp-server` | Server name identifier |
| `MCP_SERVER_VERSION` | `0.2.0` | Server version string |
| `MCP_TRANSPORT` | `http` | Transport mode: `http` (HTTP/SSE) or `stdio` |
| `MCP_HOST` | `0.0.0.0` | Host address for HTTP/SSE server |
| `MCP_PORT` | `8765` | Port for HTTP/SSE server |
| `MCP_PATH` | `/sse` | SSE endpoint path |
| `MCP_ALLOW_WRITE` | `true` | **Enable write operations** (see [Write Access Control](#write-access-control)) |
| `MCP_HTTP_TIMEOUT` | `300` | HTTP request timeout in seconds |
| `MCP_HTTP_KEEPALIVE_TIMEOUT` | `5` | HTTP keepalive timeout in seconds |
| `MCP_HTTP_CONNECT_TIMEOUT` | `60` | HTTP connection timeout in seconds (also used as initialization timeout) |
| `MCP_LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `MCP_LOG_FORMAT` | `json` | Log format: `json` or `text` |

#### Helm Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `HELM_TIMEOUT` | `300` | Timeout in seconds for Helm operations |

#### Kubernetes Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `K8S_TIMEOUT` | `30` | Timeout in seconds for Kubernetes API operations |
| `KUBECONFIG` | `~/.kube/config` | Path to kubeconfig file |

### Write Access Control

The `MCP_ALLOW_WRITE` environment variable controls whether **mutating operations** are allowed. This is a critical security feature that prevents accidental modifications to your Kubernetes cluster.

#### When `MCP_ALLOW_WRITE=true` (Default)

All operations are enabled, including:
- ✅ **Install** (`helm_install_chart`) - Install new Helm releases
- ✅ **Upgrade** (`helm_upgrade_release`) - Upgrade existing releases
- ✅ **Rollback** (`helm_rollback_release`) - Rollback to previous revisions
- ✅ **Uninstall** (`helm_uninstall_release`) - Remove Helm releases
- ✅ **Dry-run** operations - Preview changes without applying

#### When `MCP_ALLOW_WRITE=false` (Read-Only Mode)

Only **read-only operations** are allowed:
- ✅ **Discovery** - Search charts, get chart info
- ✅ **Validation** - Validate values, render manifests, check dependencies
- ✅ **Monitoring** - Get release status, list releases
- ✅ **Dry-run** operations - Preview installations without applying
- ❌ **Install** - Blocked (raises `HelmOperationError`)
- ❌ **Upgrade** - Blocked (raises `HelmOperationError`)
- ❌ **Rollback** - Blocked (raises `HelmOperationError`)
- ❌ **Uninstall** - Blocked (raises `HelmOperationError`)

**Use Case**: Set `MCP_ALLOW_WRITE=false` when you want to use the server for discovery, validation, and monitoring only, preventing any accidental deployments or modifications.

**Note**: Dry-run operations (`dry_run=True`) are always allowed, even when `MCP_ALLOW_WRITE=false`, as they don't modify the cluster.

### MCP Client Configuration

**This section shows how to configure MCP clients** (such as Claude Desktop, Cline, or other MCP-compatible applications) **to connect to and use the Helm MCP Server**. 

**Important**: The Helm MCP Server must be running before configuring your client. Start the server using one of the installation methods above, then configure your client to connect to it.

---

## 🛠️ Available Tools

### Discovery Tools

| Tool | Description |
|------|-------------|
| `helm_search_charts` | Search for Helm charts in repositories |
| `helm_get_chart_info` | Get detailed chart metadata and documentation |
| `helm_ensure_repository` | Ensure a Helm repository exists, adding it if necessary |

### Installation Tools

| Tool | Description |
|------|-------------|
| `helm_install_chart` | Install a Helm chart to cluster |
| `helm_upgrade_release` | Upgrade an existing Helm release |
| `helm_rollback_release` | Rollback to a previous revision |
| `helm_uninstall_release` | Uninstall a Helm release |
| `helm_dry_run_install` | Preview installation without deploying |

### Validation Tools

| Tool | Description |
|------|-------------|
| `helm_validate_values` | Validate chart values against schema |
| `helm_render_manifests` | Render Kubernetes manifests from chart |
| `helm_validate_manifests` | Validate rendered Kubernetes manifests |
| `helm_check_dependencies` | Check if chart dependencies are available |
| `helm_get_installation_plan` | Generate installation plan with resource estimates |

### Kubernetes Tools

| Tool | Description |
|------|-------------|
| `kubernetes_get_cluster_info` | Get cluster information |
| `kubernetes_list_namespaces` | List all Kubernetes namespaces |
| `kubernetes_list_contexts` | List all available Kubernetes contexts from kubeconfig |
| `kubernetes_set_context` | Set/switch to a specific Kubernetes context |
| `kubernetes_get_helm_releases` | List all Helm releases in cluster |
| `kubernetes_check_prerequisites` | Check cluster prerequisites |

### Monitoring Tools

| Tool | Description |
|------|-------------|
| `helm_monitor_deployment` | Monitor deployment health asynchronously |
| `helm_get_release_status` | Get current status of a Helm release |

---

## 📁 Available Resources

| Resource URI | Description |
|--------------|-------------|
| `helm://releases` | List all Helm releases in cluster |
| `helm://releases/{release_name}` | Get detailed release information |
| `helm://charts` | List available charts in repositories |
| `helm://charts/{repo}/{name}` | Get specific chart metadata |
| `helm://charts/{repo}/{name}/readme` | Get chart README documentation |
| `kubernetes://cluster-info` | Get Kubernetes cluster information |
| `kubernetes://namespaces` | List all Kubernetes namespaces |
| `helm://best_practices` | Helm Best Practices guide |

---

## 💬 Available Prompts

| Prompt | Description | Arguments |
|--------|-------------|-----------|
| `helm_workflow_guide` | Complete workflow documentation | — |
| `helm_quick_start` | Quick start for common operations | — |
| `helm_installation_guidelines` | Installation best practices | — |
| `helm_troubleshooting_guide` | Troubleshooting common issues | `error_type` |
| `helm_security_checklist` | Security considerations | — |
| `helm_upgrade_guide` | Upgrade guide for charts | `chart_name` |
| `helm_rollback_procedures` | Rollback step-by-step guide | `release_name` |

---

For more details, see the [Helm MCP Server Readme](https://github.com/talkops-ai/talkops-mcp/blob/main/src/helm-mcp-server/README.md).