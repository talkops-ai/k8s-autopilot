"""
Sub-agent specifications for the K8s Operator Deep Agent coordinator.

Each sub-agent is a ``CompiledSubAgent`` with JIT MCP connection.
The K8s Operator currently provides one domain-specific subagent:
  - ``k8s-cluster-ops``  → kubernetes-mcp-server

Extensibility:
    To add another sub-agent:
    1. Define its prompt and dict spec below.
    2. Add a HITL middleware builder in ``middleware.py``.
    3. Add it to ``get_k8s_subagent_specs()``.
"""

from typing import Any, Callable, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("K8sSubagentFactory")

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

K8S_CLUSTER_OPS_PROMPT = """\
You are the Kubernetes Cluster Operations agent.
You manage Kubernetes and OpenShift clusters via the kubernetes-mcp-server.
You rely entirely on MCP tools — never use bash/shell commands.

## STEP 1: Operation Classification — ALWAYS Do This First

Classify the request before doing ANYTHING else:

**READ-ONLY** (list pods, get resources, pod logs, events, top, contexts, health check):
→ Use the Read-Only Fast-Path below. Do NOT read any files.

**STATE-MODIFYING** (create, update, scale, delete, exec, run pod):
→ Use the Full Phased Workflow below.

## Read-Only Fast-Path (READ-ONLY operations)

For **read-only** queries, call the tool directly, format the output, and return.
Do NOT read SKILL.md, AGENTS.md, or operations-log.md for read-only queries.

| Query type | Tool |
|---|---|
| List all pods (cluster-wide) | `pods_list` |
| List pods in namespace | `pods_list_in_namespace` |
| Get pod details | `pods_get` |
| Pod logs | `pods_log` |
| Pod resource usage | `pods_top` |
| List resources | `resources_list` |
| Get resource details | `resources_get` |
| List namespaces | `namespaces_list` |
| List events | `events_list` |
| Node resource usage | `nodes_top` |
| Node stats | `nodes_stats_summary` |
| Node logs | `nodes_log` |
| List kubeconfig contexts | `configuration_contexts_list` |
| View kubeconfig | `configuration_view` |
| Check current replica count | `resources_scale` (without `scale` param) |

**CRITICAL RULES for read-only queries:**
1. Call the tool ONCE. Format the result. Return immediately.
2. If the tool returns "no results" or an error, **that IS the answer**. Report it directly.
   Do NOT retry the same tool. Do NOT try alternative tools. Do NOT search the filesystem.
3. Never call the same read-only tool more than once per request.

**Cluster Health Check:**
Use the `cluster-health-check` MCP prompt for comprehensive assessments. This is a safe,
read-only prompt that runs multiple tools automatically.

## Full Phased Workflow (STATE-MODIFYING operations only)

Use this workflow ONLY for mutations: creating/updating resources, scaling, deleting, \
exec-ing into pods, or running debug pods.

**Before starting**: Read the SKILL.md for safety rules and workflow details:
`read_file /skills/k8s-operator/kubernetes-cluster-ops/SKILL.md`

### Idempotency Rules — ALWAYS Check Before Creating

**NEVER create a resource without first checking if it already exists.**

| Before creating... | First check with... | If exists... |
|---|---|---|
| Any resource | `resources_get(apiVersion, kind, name, namespace)` | Use `resources_create_or_update` (upsert) — but warn user it will overwrite |
| Pod (via `pods_run`) | `pods_get(name, namespace)` | Report existing pod — do NOT create duplicate |
| Scale target | `resources_scale(apiVersion, kind, name, namespace)` (no scale param) | Read current replicas, confirm new target with user |

### Phase 1: Discovery
- If the task description provides resource kind, name, namespace, and action, skip to Planning.
- Otherwise: check `/memories/k8s-operator/operations-log.md` for recent operations context.
- Only as LAST RESORT, call `request_human_input` for missing parameters.

### Phase 2: Planning — MANDATORY
- **Always read before write.** Call the read variant first (`resources_get`, \
`resources_scale` without scale param, `pods_get`) to capture current state.
- Present a clear action plan to the user.
- You MUST call `request_human_input` with:

  **For create/update:**
  ```
  question="Here is the execution plan. Do you approve?"
  context="📝 **CREATE/UPDATE PLAN**\\n\\n**Kind**: {kind}\\n**Name**: {name}\\n**Namespace**: {namespace}\\n\\n```yaml\\n{yaml_preview}\\n```\\n\\n**Impact**: {description}"
  phase="create_update_plan_review"
  ```

  **For delete:**
  ```
  question="Here is the deletion plan. Do you approve?"
  context="🗑️ **DELETION PLAN**\\n\\n**Kind**: {kind}\\n**Name**: {name}\\n**Namespace**: {namespace}\\n\\n⚠️ **Impact**: {what_will_be_removed}"
  phase="deletion_plan_review"
  ```

  **For scale:**
  ```
  question="Here is the scaling plan. Do you approve?"
  context="⚖️ **SCALE PLAN**\\n\\n**Kind**: {kind}\\n**Name**: {name}\\n**Current replicas**: {current}\\n**Target replicas**: {target}\\n**Namespace**: {namespace}"
  phase="scale_plan_review"
  ```

  **For exec:**
  ```
  question="Approve shell access to this pod?"
  context="🔐 **POD EXEC**\\n\\n**Pod**: {pod_name}\\n**Container**: {container}\\n**Command**: `{cmd}`\\n**Namespace**: {namespace}\\n\\n⚠️ This grants shell-level access to the container."
  phase="exec_approval"
  ```

- WAIT for approval before proceeding.

### Phase 3: Execution
- Tools are additionally gated by `HumanInTheLoopMiddleware` as a background safety net.
- For `resources_create_or_update`: show YAML before applying.
- For `pods_exec`: confirm exact command before running.

### Phase 4: Verification
- After mutation, re-read the resource to confirm the change took effect.
- For scale: confirm `readyReplicas` matches target.
- For delete: confirm resource no longer exists (may get 404).
- Do NOT declare success based solely on tool stdout.

Return: "Completed K8s cluster operation: {summary}".
CRITICAL: Do NOT use `request_human_input` to report final success or summaries. Just return the final raw text string!
"""

# ---------------------------------------------------------------------------
# Sub-agent spec dict
# ---------------------------------------------------------------------------

K8S_CLUSTER_OPS_SUBAGENT: dict[str, Any] = {
    "name": "k8s-cluster-ops",
    "description": (
        "Specialized agent for Kubernetes cluster operations: listing/inspecting resources, "
        "pod logs and exec, applying YAML manifests, scaling deployments, deleting resources, "
        "running debug pods, viewing events, node diagnostics, cluster health checks, "
        "and kubeconfig context management. "
        "Connects directly to the kubernetes_mcp_server."
    ),
    "system_prompt": K8S_CLUSTER_OPS_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}


# ---------------------------------------------------------------------------
# JIT MCP Subagent Wrapper
# ---------------------------------------------------------------------------

def _build_mcp_subagent(
    spec: dict[str, Any],
    coordinator_model_name: str,
    *,
    server_filter: list[str],
    mcp_resource_server_name: str,
    include_filesystem: bool = False,
    hitl_builder: Optional[Callable[[], Any]] = None,
) -> Any:  # CompiledSubAgent
    """Wraps a static dict spec into a dynamic CompiledSubAgent that opens its
    MCP connection Just-In-Time (JIT) specifically when its node is executed.

    Args:
        spec: Static subagent dict (name, description, system_prompt).
        coordinator_model_name: Model name string.
        server_filter: MCP server names to connect to.
        mcp_resource_server_name: Server name passed to ``read_mcp_resource``.
        include_filesystem: If True, attach ``FilesystemMiddleware``.
        hitl_builder: Callable that returns a ``HumanInTheLoopMiddleware``
            instance. If None, no HITL middleware is attached.
    """
    from langchain_core.runnables import RunnableLambda
    from langchain_core.runnables.config import RunnableConfig
    from deepagents.middleware.subagents import CompiledSubAgent

    name = spec["name"]
    description = spec.get("description", "")
    system_prompt = spec.get("system_prompt", "")

    async def _mcp_runnable(
        state: dict[str, Any],
        config: RunnableConfig,
    ) -> dict[str, Any]:
        from k8s_autopilot.utils.mcp_client import create_mcp_client
        from k8s_autopilot.config.config import Config
        from k8s_autopilot.utils.llm import create_model
        from langchain.agents import create_agent

        # Lazily connect to MCP right before execution
        async with create_mcp_client(Config(), server_filter=server_filter) as mcp_client:
            tools = mcp_client.get_tools()

            from k8s_autopilot.core.hitl.tools import create_hitl_tools
            from langchain_core.tools import StructuredTool

            # Generic MCP resource reader — parameterized by server_name
            _res_server = mcp_resource_server_name

            async def read_mcp_resource(uri: str) -> str:
                """Read content of a specific MCP resource by URI."""
                try:
                    res = await mcp_client.read_resource(uri, server_name=_res_server)
                    if hasattr(res, 'contents') and res.contents:
                        for item in res.contents:
                            if hasattr(item, 'text'):
                                return item.text
                    return str(res)
                except Exception as e:
                    return f"Error reading resource {uri}: {str(e)}"

            tools.extend(create_hitl_tools())
            tools.append(
                StructuredTool.from_function(
                    func=None,
                    coroutine=read_mcp_resource,
                    name="read_mcp_resource",
                    description=(
                        "Read content of a specific MCP resource by URI "
                        f"(server: {_res_server}). Use this to read state natively."
                    ),
                )
            )

            # Build middleware list
            middleware = []
            if include_filesystem:
                from deepagents.middleware.filesystem import FilesystemMiddleware
                from deepagents.backends import FilesystemBackend
                from k8s_autopilot.utils.memory import get_project_root

                root = str(get_project_root())
                middleware.append(
                    FilesystemMiddleware(
                        backend=FilesystemBackend(
                            root_dir=root,
                            virtual_mode=True,
                        ),
                        custom_tool_descriptions={
                            "read_file": (
                                "Read a file from the workspace filesystem. "
                                "Use this to read skills and other textual context."
                            ),
                            "ls": "List files in a workspace directory."
                        },
                    )
                )

            if hitl_builder is not None:
                from langchain.agents.middleware import ToolRetryMiddleware

                class CustomToolRetryMiddleware(ToolRetryMiddleware):
                    def _should_retry_tool(self, tool_name: str) -> bool:
                        # Never retry HITL tools — GraphInterrupt must propagate.
                        if tool_name == "request_human_input":
                            return False
                        return super()._should_retry_tool(tool_name)

                middleware.append(hitl_builder())
                middleware.append(
                    CustomToolRetryMiddleware(
                        max_retries=2,
                        backoff_factor=1.5,
                        initial_delay=0.5,
                        max_delay=10.0,
                        on_failure="continue",
                    )
                )
                _subagent_logger.info(
                    f"{name}: attached HumanInTheLoopMiddleware + ToolRetryMiddleware"
                )

            # Lazily instantiate model and graph
            cfg = Config()
            model = create_model(cfg.get_llm_deepagent_config())
            agent_graph = create_agent(
                model=model,
                tools=tools,
                middleware=middleware,
                system_prompt=system_prompt,
                name=name,
            )

            from typing import cast
            result = await agent_graph.ainvoke(cast(Any, state), config)
            return dict(result)

    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=RunnableLambda(_mcp_runnable).with_config({"run_name": name}),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_k8s_subagent_specs(
    coordinator_model: Any = None,
) -> list[Any]:
    """Assemble sub-agent specs for the K8s Operator deep agent.

    Returns the K8s Cluster Ops sub-agent as a JIT-connected MCP CompiledSubAgent.
    """
    from k8s_autopilot.core.agents.k8s_operator.middleware import (
        build_k8s_cluster_ops_hitl_middleware,
    )

    coord_model = coordinator_model or ""

    return [
        # Kubernetes Cluster Operations sub-agent
        _build_mcp_subagent(
            K8S_CLUSTER_OPS_SUBAGENT,
            str(coord_model),
            server_filter=["kubernetes_mcp_server"],
            mcp_resource_server_name="kubernetes_mcp_server",
            include_filesystem=True,
            hitl_builder=build_k8s_cluster_ops_hitl_middleware,
        ),
    ]
