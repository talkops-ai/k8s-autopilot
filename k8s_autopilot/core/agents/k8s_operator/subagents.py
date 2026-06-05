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

from k8s_autopilot.core.agents.k8s_operator.prompt_sections import (
    compose_subagent_prompt,
    create_subagent_registry,
)

# ---------------------------------------------------------------------------
# Composed subagent prompts (from prompt_sections.py)
#
# Each prompt is assembled from modular, testable blocks via PromptRegistry.
# Shared boilerplate (<scope>, <plan_locked_protocol>, <output_contract>,
# Iron Rules) is defined ONCE in prompt_sections.py and composed per-domain
# with domain-specific routing tables, safety rules, and workflow phases.
#
# To customise at runtime:
#     registry = create_subagent_registry("k8s-cluster-ops", safety_rules="<custom>")
#     custom_prompt = registry.compose()
# ---------------------------------------------------------------------------

K8S_CLUSTER_OPS_PROMPT = compose_subagent_prompt("k8s-cluster-ops")

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
            middleware: list[Any] = []
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
