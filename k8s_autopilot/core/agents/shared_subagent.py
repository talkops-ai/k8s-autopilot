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

# ---------------------------------------------------------------------------
# Subagent loop-prevention limits (configurable via environment variables)
#
# These follow the LangChain "Going to Production" recommendation:
#   https://docs.langchain.com/oss/python/deepagents/going-to-production#rate-limiting
#
# The AGENTS.md step budget says a single read-only query should use 3-5
# steps, and a simple mutation 8-12.  These limits are generous enough for
# complex multi-step investigations while still stopping 150-call loops.
# ---------------------------------------------------------------------------
import os
from pathlib import Path

_SUBAGENT_TOOL_CALL_LIMIT = int(
    os.getenv("SUBAGENT_TOOL_CALL_LIMIT", "25")
)
_SUBAGENT_MODEL_CALL_LIMIT = int(
    os.getenv("SUBAGENT_MODEL_CALL_LIMIT", "20")
)
_SUBAGENT_DISCOVERY_TOOL_LIMIT = int(
    os.getenv("SUBAGENT_DISCOVERY_TOOL_LIMIT", "3")
)

# High-frequency discovery tools that should never be called >3 times per
# task.  Covers Loki label enumeration and Prometheus metric exploration.
_DISCOVERY_TOOL_CAP_LIST = [
    "get_label_values",
    "get_cluster_labels",
    "get_active_series",
    "get_detected_fields",
    "prom_explore_labels",
]


def _derive_subagent_memory_paths(skills: List[str]) -> List[str]:
    """Dynamically derive scoped memory sources from a subagent's skill list.

    Follows the dcode pattern: ``MemoryMiddleware`` receives a standalone
    ``FilesystemBackend(virtual_mode=False)`` and ``sources`` is a list of
    **real filesystem paths** to AGENTS.md and other policy files.
    """
    from k8s_autopilot.core.backend import get_project_root
    from k8s_autopilot.core.memory import get_memory_registry

    registry = get_memory_registry()
    paths: set[str] = set()

    # Always include user preferences (dcode: settings.get_user_agent_md_path)
    user_agents = Path.home() / ".agents" / "AGENTS.md"
    if user_agents.exists():
        paths.add(str(user_agents))

    # Project-level AGENTS.md
    project_agents = get_project_root() / "AGENTS.md"
    if project_agents.exists():
        paths.add(str(project_agents))

    for skill_path in skills:
        # Parse: /skills/{domain}/{role} → domain, role
        parts = [p for p in skill_path.strip("/").split("/") if p]
        if len(parts) >= 2:
            domain = parts[1]  # e.g., "helm-operator"
            role = parts[2] if len(parts) >= 3 else None  # e.g., "helm-operation"

            # Resolve virtual memory paths using the centralized MemoryRegistry
            virtual_paths = registry.get_memory_paths_for_domain_and_role(domain, role)
            for vp in virtual_paths:
                phys = registry.resolve_virtual_path(vp)
                if phys and phys.is_file():
                    paths.add(str(phys))

    return sorted(list(paths))


def build_mcp_subagent(
    spec: Dict[str, Any],
    *,
    server_filter: List[str],
    mcp_resource_server_name: str,
    include_filesystem: bool = False,
    skill_paths: Optional[List[str]] = None,
    memory_paths: Optional[List[str]] = None,
    hitl_builder: Optional[Callable[[], Any]] = None,
    resource_description_override: Optional[str] = None,
    extra_middleware_builders: Optional[List[Callable[[], Any]]] = None,
    extra_tools: Optional[List[Any]] = None,
) -> Any:  # CompiledSubAgent
    """Wrap a static subagent dict spec into a JIT-connected ``CompiledSubAgent``.

    The returned ``CompiledSubAgent`` lazily opens its MCP connection only when
    the coordinator's LangGraph dispatches a ``task(subagent_name, ...)`` call.

    Skills and memory are **dynamically derived** from ``spec["skills"]`` or parameters:
    - Skills isolation: Only the skill directories listed in ``spec["skills"]``
      are visible to ``SkillsMiddleware`` (prevents cross-bleeding).
    - Memory scoping: Domain and role are extracted from skill paths to load
      only role-specific memory (prevents context bloat).

    Args:
        spec: Static subagent dict with ``name``, ``description``,
            ``system_prompt``, and ``skills`` (list of virtual skill paths
            like ``["/skills/helm-operator/helm-operation"]``).
        server_filter: MCP server names to connect to (e.g. ``["prometheus-mcp-server"]``).
        mcp_resource_server_name: Server name for the ``read_mcp_resource`` tool.
        include_filesystem: Attach ``SkillsMiddleware`` + ``FilesystemMiddleware``
            scoped to the spec's ``skills`` list.  **SkillsMiddleware is always
            added BEFORE FilesystemMiddleware** per official docs ordering.
        skill_paths: Optional override for virtual skill paths.
        memory_paths: Optional override for physical memory paths.
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
    # Derive skill and memory scope from the spec's skills key or overrides
    subagent_skills = skill_paths or spec.get("skills", [])
    subagent_memory = memory_paths or _derive_subagent_memory_paths(subagent_skills)

    async def _mcp_runnable(
        state: Dict[str, Any],
        config: RunnableConfig,
    ) -> Dict[str, Any]:
        from k8s_autopilot.utils.mcp_client import create_mcp_client
        from k8s_autopilot.config.config import Config
        from k8s_autopilot.utils.llm import create_model
        from langchain.agents import create_agent

        try:
            cfg = (
                config.get("configurable", {}).get("app_config")
                if isinstance(config, dict)
                else None
            ) or Config()

            # Lazily connect to MCP right before execution
            async with create_mcp_client(cfg, server_filter=server_filter) as mcp_client:
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
                        subagent_skills=subagent_skills,
                        memory_paths=subagent_memory,
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

                # ── Tier 1: Hard middleware limits (LangChain built-in) ────
                # Per LangChain docs ("Going to Production"):
                #   "Without limits, a confused agent can burn through your
                #    LLM API budget in minutes by looping on the same tool
                #    call or making hundreds of model calls. Set caps on
                #    BOTH model calls and tool executions per run."
                #
                # exit_behavior="continue" lets the subagent still produce
                # a final summary after hitting the limit, instead of
                # silently dying (which caused the "stuck agent" bug with
                # exit_behavior="end" on the coordinator).
                #
                # NOTE: ToolCallLimitMiddleware supports "continue" | "end" | "error".
                #       ModelCallLimitMiddleware only supports "end" | "error".
                #       We use "end" for ModelCallLimit (graceful termination with
                #       AI summary message) and "continue" for ToolCallLimit (agent
                #       continues but blocked calls get error messages).
                # Ref: https://docs.langchain.com/oss/python/langchain/middleware/built-in#tool-call-limit
                from langchain.agents.middleware import (
                    ToolCallLimitMiddleware,
                    ModelCallLimitMiddleware,
                )

                middleware.append(
                    ToolCallLimitMiddleware(
                        run_limit=_SUBAGENT_TOOL_CALL_LIMIT,
                        exit_behavior="continue",
                    )
                )
                middleware.append(
                    ModelCallLimitMiddleware(
                        run_limit=_SUBAGENT_MODEL_CALL_LIMIT,
                        exit_behavior="end",
                    )
                )


                # Per-tool caps for high-frequency discovery tools.
                # These tools are called for label/series enumeration and
                # should never need >3 calls per task. Stops pathological
                # loops like "call get_label_values 40 times".
                for _disco_tool in _DISCOVERY_TOOL_CAP_LIST:
                    middleware.append(
                        ToolCallLimitMiddleware(
                            tool_name=_disco_tool,
                            run_limit=_SUBAGENT_DISCOVERY_TOOL_LIMIT,
                            exit_behavior="continue",
                        )
                    )

                # ── Tier 2: Duplicate call guard (Claude Code PreToolUse) ─
                # Hashes (tool_name, args) and returns cached result for
                # identical repeat calls.
                from k8s_autopilot.core.middleware.registry import get_middleware_registry
                middleware.extend(get_middleware_registry().build_middlewares(["duplicate_tool_call_guard"]))

                _logger.info(
                    f"{name}: Loop prevention middleware attached "
                    f"(tool_limit={_SUBAGENT_TOOL_CALL_LIMIT}, "
                    f"model_limit={_SUBAGENT_MODEL_CALL_LIMIT}, "
                    f"discovery_cap={_SUBAGENT_DISCOVERY_TOOL_LIMIT}, "
                    f"dedup=on)"
                )

                model = create_model(cfg.get_llm_deepagent_config())
                agent_graph = create_agent(
                    model=model,
                    tools=tools,
                    middleware=middleware,
                    system_prompt=system_prompt,
                    name=name,
                )


                # Build a clean child config to avoid conflicting with the parent's checkpointer/runtime
                child_config: RunnableConfig = {
                    "configurable": {
                        "ls_agent_type": "subagent",
                    }
                }
                # Preserve app_config for JIT connection initialization
                parent_configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
                if "app_config" in parent_configurable:
                    child_config["configurable"]["app_config"] = parent_configurable["app_config"]

                from typing import cast
                result = await agent_graph.ainvoke(cast(Any, state), child_config)
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
    subagent_skills: Optional[List[str]] = None,
    memory_paths: Optional[List[str]] = None,
) -> None:
    """Attach SkillsMiddleware + MemoryMiddleware + FilesystemMiddleware.

    Follows the **dcode pattern**: ``SkillsMiddleware`` and ``MemoryMiddleware``
    each receive their own standalone ``FilesystemBackend(virtual_mode=False)``
    pointing to real filesystem paths.  No virtual routing or CompositeBackend.

    **Skills isolation**: ``SkillsMiddleware.sources`` is set to the real
    parent directory of each target skill (e.g., ``<root>/skills/helm-operator/``)
    but with a ``FilesystemBackend`` rooted at an isolated temp directory that
    contains only symlinks to the target skill subdirectories.  This prevents
    cross-bleeding.

    The global ``CompositeBackend`` (from ``make_backend()``) is used only for
    ``FilesystemMiddleware`` (file tools like ``read_file``, ``ls``, etc.).

    **Ordering**: SkillsMiddleware and MemoryMiddleware MUST be added BEFORE
    FilesystemMiddleware.

    Ref: dcode/code/agent.py lines 1580-1648
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware import SkillsMiddleware
    from deepagents import MemoryMiddleware
    from deepagents.backends.filesystem import FilesystemBackend
    from k8s_autopilot.core.backend import K8sBackendMixin, get_project_root

    root = get_project_root()
    plugins_root = root / "plugins"

    # ── SkillsMiddleware (dcode pattern: standalone FilesystemBackend) ──
    # Derive real filesystem source dirs from skill virtual paths
    # e.g., ["/skills/helm-operator/helm-operation"] → ["<root>/plugins/helm-operator/agents/helm-operation/skills"]
    if subagent_skills:
        # Create isolated temp dir with symlinks to only the target skills
        import tempfile
        isolated_dir = Path(tempfile.mkdtemp(prefix="k8s_skills_"))

        source_dirs: list[str] = []
        parent_set: dict[str, None] = {}

        for skill_vpath in subagent_skills:
            parts = [p for p in skill_vpath.strip("/").split("/") if p]
            if len(parts) < 3:
                continue
            # parts = ["skills", "helm-operator", "helm-operation"]
            domain = parts[1]
            role = parts[2]
            source = plugins_root / domain / "agents" / role / "skills"
            target = isolated_dir / domain / role

            if source.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.symlink_to(source)

            # Source dir for SkillsMiddleware = parent of the skill dir in the isolated structure
            parent_key = str(isolated_dir / domain)
            parent_set[parent_key] = None

        source_dirs = sorted(parent_set.keys())
        skills_backend = FilesystemBackend(virtual_mode=False)

        _logger.info(
            f"{name}: SkillsMiddleware — isolated ({subagent_skills}), "
            f"sources: {source_dirs}"
        )
    else:
        source_dirs = [str(plugins_root)]
        skills_backend = FilesystemBackend(virtual_mode=False)

        _logger.info(
            f"{name}: SkillsMiddleware — all skills, "
            f"sources: {source_dirs}"
        )

    middleware.append(
        SkillsMiddleware(
            backend=skills_backend,
            sources=source_dirs,
        )
    )

    # ── MemoryMiddleware (dcode pattern: standalone FilesystemBackend) ──
    if memory_paths:
        memory_backend = FilesystemBackend(virtual_mode=False)
        middleware.append(
            MemoryMiddleware(
                backend=memory_backend,
                sources=sorted(memory_paths),
            )
        )
        _logger.info(
            f"{name}: MemoryMiddleware attached "
            f"(sources: {sorted(memory_paths)})"
        )

    # ── FilesystemMiddleware — uses the global CompositeBackend ──
    _global_backend = K8sBackendMixin.make_backend()
    _paths_desc = ", ".join(f"`{d}`" for d in source_dirs)
    middleware.append(
        FilesystemMiddleware(
            backend=_global_backend,
            custom_tool_descriptions={
                "read_file": (
                    f"Read a file from the workspace filesystem. "
                    f"ONLY use this to read skill files under {_paths_desc} "
                    f"and memory files under `/memories/`. "
                    f"Do NOT use this tool for any other purpose."
                ),
                "ls": (
                    f"List files in a skill or memory directory. "
                    f"Allowed paths: {_paths_desc} and `/memories/`. "
                    f"Do NOT call `ls` on `/`, `/.venv/`, or any project directory."
                ),
                "glob": (
                    f"Glob files within skill or memory directories ONLY: {_paths_desc}, `/memories/`. "
                    f"Do NOT glob across the entire workspace or log files."
                ),
                "grep": (
                    f"Search within skill or memory files ONLY: {_paths_desc}, `/memories/`. "
                    f"Do NOT grep log files, `.venv`, or workspace source code."
                ),
            },
        )
    )
    _logger.info(
        f"{name}: FilesystemMiddleware attached "
        f"(allowed: {_paths_desc})"
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
