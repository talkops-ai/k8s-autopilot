"""
Shared JIT MCP subagent builder for ALL deep agent coordinators.

Provides a single ``build_mcp_subagent()`` function that replaces the 4
duplicated ``_build_mcp_subagent()`` copies across observability, app_operator,
k8s_operator, and helm_operator.

Key capabilities:
    - JIT MCP connection via ``create_mcp_client`` context manager
    - ``SkillsMiddleware`` — progressive disclosure of SKILL.md metadata
    - ``FilesystemMiddleware`` — scoped file access (``read_file``, ``ls``, etc.)
    - ``HumanInTheLoopMiddleware`` — via caller-supplied builder
    - ``CustomToolRetryMiddleware`` — HITL-safe retry (skips ``request_human_input``)
    - ``read_mcp_resource`` tool — parameterized by MCP server name
    - Graceful error handling for MCP connection failures

Usage::

    from k8s_autopilot.core.agents.shared_subagent import build_mcp_subagent

    subagent = build_mcp_subagent(
        PROMETHEUS_OPERATOR_SUBAGENT,
        server_filter=["prometheus-mcp-server"],
        mcp_resource_server_name="prometheus-mcp-server",
        include_filesystem=True,
        skill_paths=["/skills/observability/prometheus/"],
        hitl_builder=build_prometheus_hitl_middleware,
    )

API References:
    - CompiledSubAgent:
      https://docs.langchain.com/oss/python/deepagents/customization#compiled-subagents
    - SkillsMiddleware:
      https://docs.langchain.com/oss/python/langchain/agents#context-management
    - FilesystemMiddleware:
      https://docs.langchain.com/oss/python/deepagents/customization#default-stack-main-agent
"""

from typing import Any, Callable, Dict, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

_logger = AgentLogger("SharedSubagentFactory")


def build_mcp_subagent(
    spec: Dict[str, Any],
    *,
    server_filter: List[str],
    mcp_resource_server_name: str,
    include_filesystem: bool = False,
    skill_paths: Optional[List[str]] = None,
    hitl_builder: Optional[Callable[[], Any]] = None,
    resource_description_override: Optional[str] = None,
    extra_middleware_builders: Optional[List[Callable[[], Any]]] = None,
    extra_tools: Optional[List[Any]] = None,
) -> Any:  # CompiledSubAgent
    """Wrap a static subagent dict spec into a JIT-connected ``CompiledSubAgent``.

    The returned ``CompiledSubAgent`` lazily opens its MCP connection only when
    the coordinator's LangGraph dispatches a ``task(subagent_name, ...)`` call.

    Args:
        spec: Static subagent dict with ``name``, ``description``, ``system_prompt``.
        server_filter: MCP server names to connect to (e.g. ``["prometheus-mcp-server"]``).
        mcp_resource_server_name: Server name for the ``read_mcp_resource`` tool.
        include_filesystem: Attach ``SkillsMiddleware`` + ``FilesystemMiddleware``
            scoped to ``skill_paths``.  **SkillsMiddleware is always added BEFORE
            FilesystemMiddleware** per official docs ordering.
        skill_paths: Virtual paths the subagent may access (e.g.
            ``["/skills/observability/prometheus/"]``).  Also used by
            ``SkillsMiddleware`` for progressive disclosure of SKILL.md frontmatter.
            Defaults to ``["/skills/"]``.
        hitl_builder: Callable returning a ``HumanInTheLoopMiddleware`` instance.
            If provided, a HITL-safe ``ToolRetryMiddleware`` is also attached.
        resource_description_override: Custom description for the ``read_mcp_resource``
            tool.  If None, uses a generic default parameterized by server name.
        extra_middleware_builders: Additional callables returning middleware instances.
            Appended after filesystem/HITL middleware.  Use for domain-specific
            concerns not covered by the shared builder.
        extra_tools: Additional tool instances (e.g. ``create_kubectl_readonly_tool()``)
            to inject alongside MCP tools.  Appended after MCP + HITL + resource
            tools but before ``create_agent()`` is called.  Use this generic
            extension point to give any subagent custom capabilities without
            modifying the shared builder.

    Returns:
        A ``CompiledSubAgent`` wrapping the JIT runnable.
    """
    from langchain_core.runnables import RunnableLambda
    from langchain_core.runnables.config import RunnableConfig
    from deepagents.middleware.subagents import CompiledSubAgent

    name = spec["name"]
    description = spec.get("description", "")
    system_prompt = spec.get("system_prompt", "")

    async def _mcp_runnable(
        state: Dict[str, Any],
        config: RunnableConfig,
    ) -> Dict[str, Any]:
        from k8s_autopilot.utils.mcp_client import create_mcp_client
        from k8s_autopilot.config.config import Config
        from k8s_autopilot.utils.llm import create_model
        from langchain.agents import create_agent

        try:
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

                _res_desc = resource_description_override or (
                    "Read content of a specific MCP resource by URI "
                    f"(server: {_res_server}). Use this to read state natively."
                )
                tools.append(
                    StructuredTool.from_function(
                        func=None,
                        coroutine=read_mcp_resource,
                        name="read_mcp_resource",
                        description=_res_desc,
                    )
                )

                # Build middleware list
                middleware: List[Any] = []
                if include_filesystem:
                    _build_filesystem_middleware(
                        middleware=middleware,
                        name=name,
                        skill_paths=skill_paths,
                    )

                if hitl_builder is not None:
                    _build_hitl_middleware(
                        middleware=middleware,
                        name=name,
                        hitl_builder=hitl_builder,
                    )

                # Extra domain-specific middleware (e.g. interpreter for PTC)
                # Builders may return None to signal graceful degradation
                # (e.g. langchain-quickjs not installed).
                if extra_middleware_builders:
                    for builder in extra_middleware_builders:
                        mw = builder()
                        if mw is not None:
                            middleware.append(mw)

                # Inject extra tools (e.g. kubectl_readonly for diagnostics)
                # These are appended after MCP + HITL + resource tools so they
                # appear alongside domain tools in the agent's tool list.
                if extra_tools:
                    tools.extend(extra_tools)
                    _logger.info(
                        f"{name}: {len(extra_tools)} extra tool(s) injected: "
                        f"{[getattr(t, 'name', str(t)) for t in extra_tools]}"
                    )

                # Lazily instantiate model and graph — prefer coordinator's config
                # over a fresh Config() to ensure sub-agents inherit model/backend
                # settings from the coordinator.
                cfg = (
                    config.get("configurable", {}).get("app_config")
                    if isinstance(config, dict)
                    else None
                ) or Config()
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

        except Exception as exc:
            # ── Let HITL interrupts propagate normally ────────────────
            from langgraph.errors import GraphInterrupt
            if isinstance(exc, GraphInterrupt):
                raise

            # ── Surface MCP connection failures gracefully ────────────
            from langchain_core.messages import AIMessage

            err_str = str(exc)
            _logger.error(
                f"{name}: MCP subagent execution failed",
                extra={"error": err_str, "servers": server_filter},
            )

            if any(kw in err_str.lower() for kw in (
                "authentication failed", "401", "403",
                "unauthorized", "forbidden", "expired",
            )):
                error_msg = (
                    f"FAILED: {name} could not connect to the MCP server "
                    f"({', '.join(server_filter)}). The authentication token "
                    f"appears to be expired or invalid. Please check your "
                    f"credentials and environment variables."
                )
            else:
                error_msg = (
                    f"FAILED: {name} encountered an error: {err_str}. "
                    f"The MCP server(s) {server_filter} may be unreachable."
                )

            messages = list(state.get("messages", []))
            messages.append(AIMessage(content=error_msg))
            return {**state, "messages": messages}

    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=RunnableLambda(_mcp_runnable).with_config({"run_name": name}),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_filesystem_middleware(
    *,
    middleware: List[Any],
    name: str,
    skill_paths: Optional[List[str]] = None,
) -> None:
    """Attach SkillsMiddleware + FilesystemMiddleware to a subagent stack.

    **Ordering**: SkillsMiddleware MUST be added BEFORE FilesystemMiddleware
    so skill metadata (name + description from SKILL.md frontmatter) is
    injected into the system prompt before filesystem tools run.

    Ref: https://docs.langchain.com/oss/python/deepagents/customization#default-stack-main-agent
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware import SkillsMiddleware
    from deepagents.backends import FilesystemBackend
    from k8s_autopilot.utils.memory import get_project_root

    root = str(get_project_root())
    _skill_dirs = skill_paths or ["/skills/"]
    _paths_str = ", ".join(f"`{p}`" for p in _skill_dirs)

    # Shared backend for both skills and filesystem access
    _fs_backend = FilesystemBackend(
        root_dir=root,
        virtual_mode=True,
    )

    # ── SkillsMiddleware — progressive disclosure of SKILL.md ──
    # API: SkillsMiddleware(backend=..., sources=[...])
    # Ref: https://docs.langchain.com/oss/python/langchain/agents#context-management
    middleware.append(
        SkillsMiddleware(
            backend=_fs_backend,
            sources=_skill_dirs,
        )
    )
    _logger.info(
        f"{name}: SkillsMiddleware attached "
        f"(sources: {_skill_dirs})"
    )

    # ── FilesystemMiddleware — scoped file access ──
    middleware.append(
        FilesystemMiddleware(
            backend=_fs_backend,
            custom_tool_descriptions={
                "read_file": (
                    f"Read a file from the workspace filesystem. "
                    f"ONLY use this to read skill files under {_paths_str} "
                    f"and memory files under `/memories/`. "
                    f"Do NOT use this tool for any other purpose."
                ),
                "ls": (
                    f"List files in a skill or memory directory. "
                    f"Allowed paths: {_paths_str} and `/memories/`. "
                    f"Do NOT call `ls` on `/`, `/.venv/`, or any project directory."
                ),
                "glob": (
                    f"Glob files within skill or memory directories ONLY: {_paths_str}, `/memories/`. "
                    f"Do NOT glob across the entire workspace or log files."
                ),
                "grep": (
                    f"Search within skill or memory files ONLY: {_paths_str}, `/memories/`. "
                    f"Do NOT grep log files, `.venv`, or workspace source code."
                ),
            },
        )
    )
    _logger.info(
        f"{name}: FilesystemMiddleware attached "
        f"(allowed: {_paths_str})"
    )


def _build_hitl_middleware(
    *,
    middleware: List[Any],
    name: str,
    hitl_builder: Callable[[], Any],
) -> None:
    """Attach HITL middleware + HITL-safe ToolRetryMiddleware."""
    from langchain.agents.middleware import ToolRetryMiddleware

    class _HITLSafeRetryMiddleware(ToolRetryMiddleware):
        """ToolRetryMiddleware that never retries HITL tools.

        ``request_human_input`` raises ``GraphInterrupt`` which must
        propagate up to the coordinator/supervisor — not be retried.
        """

        def _should_retry_tool(self, tool_name: str) -> bool:
            if tool_name == "request_human_input":
                return False
            return super()._should_retry_tool(tool_name)

    middleware.append(hitl_builder())
    middleware.append(
        _HITLSafeRetryMiddleware(
            max_retries=2,
            backoff_factor=1.5,
            initial_delay=0.5,
            max_delay=10.0,
            on_failure="continue",
        )
    )
    _logger.info(
        f"{name}: HumanInTheLoopMiddleware + ToolRetryMiddleware attached"
    )
