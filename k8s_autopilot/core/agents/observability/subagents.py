"""
Sub-agent specifications for the Observability Deep Agent coordinator.

Each sub-agent is a ``CompiledSubAgent`` that JIT-connects to its
respective MCP server when executed.  The Observability coordinator
provides domain-specific subagents:
  - ``prometheus-operator``    → Prometheus MCP Server
  - ``alertmanager-operator``  → Alertmanager MCP Server
  - ``opentelemetry-operator`` → OpenTelemetry MCP Server
  - ``loki-operator``          → Loki MCP Server (read-only, no HITL)
  - ``tempo-operator``         → Tempo MCP Server (2 CRD tools need HITL)

Extensibility:
    To add another sub-agent (e.g. Grafana):
    1. Define its prompt and dict spec below.
    2. Add a HITL middleware builder in ``middleware.py`` (if state-modifying).
    3. Add it to ``get_obs_subagent_specs()``.
"""

from typing import Any, Callable, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("ObsSubagentFactory")

from k8s_autopilot.core.agents.observability.prompt_sections import (
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
#     registry = create_subagent_registry("prometheus", safety_rules="<custom>")
#     custom_prompt = registry.compose()
# ---------------------------------------------------------------------------

PROMETHEUS_OPERATOR_PROMPT = compose_subagent_prompt("prometheus")
ALERTMANAGER_OPERATOR_PROMPT = compose_subagent_prompt("alertmanager")
OPENTELEMETRY_OPERATOR_PROMPT = compose_subagent_prompt("opentelemetry")
LOKI_OPERATOR_PROMPT = compose_subagent_prompt("loki")
TEMPO_OPERATOR_PROMPT = compose_subagent_prompt("tempo")



# ---------------------------------------------------------------------------
# Static sub-agent dict specs (consumed by _build_mcp_subagent)
# ---------------------------------------------------------------------------

PROMETHEUS_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "prometheus-operator",
    "description": (
        "Manages Prometheus monitoring: PromQL queries, metric exploration, "
        "exporter lifecycle (install/uninstall/verify), ServiceMonitor "
        "creation, TSDB cardinality analysis, alerting/recording rule "
        "authoring and simulation, file_sd management, and remote-write "
        "configuration. Routes to the Prometheus MCP Server."
    ),
    "system_prompt": PROMETHEUS_OPERATOR_PROMPT,
}

ALERTMANAGER_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "alertmanager-operator",
    "description": (
        "Manages Alertmanager operations: on-call alert triage and "
        "summarization, silence lifecycle (preview → validate → create → "
        "update → expire), routing introspection and audit, integration "
        "testing (test alert push), and governance/compliance review. "
        "Routes to the Alertmanager MCP Server."
    ),
    "system_prompt": ALERTMANAGER_OPERATOR_PROMPT,
}

OPENTELEMETRY_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "opentelemetry-operator",
    "description": (
        "Manages OpenTelemetry pipelines and instrumentation: "
        "collector provisioning, service onboarding (auto-instrumentation), "
        "metric cardinality auditing, sampling optimization, and security posture. "
        "Routes to the OpenTelemetry MCP Server."
    ),
    "system_prompt": OPENTELEMETRY_OPERATOR_PROMPT,
}

LOKI_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "loki-operator",
    "description": (
        "Manages Grafana Loki log observability: label schema discovery, "
        "log structure analysis (fields, patterns, parsers), LogQL query "
        "construction and execution, cost-aware query preflight, trace-log "
        "correlation, and incident response log analysis. All operations "
        "are read-only. Routes to the Loki MCP Server."
    ),
    "system_prompt": LOKI_OPERATOR_PROMPT,
}

TEMPO_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "tempo-operator",
    "description": (
        "Manages Grafana Tempo distributed tracing: TraceQL query building "
        "and execution, trace search and retrieval, trace summarization "
        "(critical path, error detection, root cause), trace comparison, "
        "RED metrics from spans (rate/errors/P99), service topology mapping, "
        "cross-pillar pivots (metrics→traces, logs→traces), PromQL alerting "
        "expression generation, backend diagnostics, and Tempo Operator CRD "
        "lifecycle (create/patch TempoStack and TempoMonolithic). Routes to "
        "the Tempo MCP Server."
    ),
    "system_prompt": TEMPO_OPERATOR_PROMPT,
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
    skill_paths: Optional[list[str]] = None,
    hitl_builder: Optional[Callable[[], Any]] = None,
) -> Any:  # CompiledSubAgent
    """Wraps a static dict spec into a dynamic CompiledSubAgent that opens its
    MCP connection Just-In-Time (JIT) specifically when its node is executed.

    Args:
        spec: Static subagent dict (name, description, system_prompt).
        coordinator_model_name: Model name string.
        server_filter: MCP server names to connect to.
        mcp_resource_server_name: Server name passed to ``read_mcp_resource``.
        include_filesystem: If True, attach ``FilesystemMiddleware`` scoped to
            ``skill_paths`` and ``/memories/`` only — prevents the subagent from
            crawling the project root, log files, or ``.venv``.
        skill_paths: List of specific skill directory paths this subagent may
            read (e.g. ``["/skills/observability/prometheus/"]``).
            When ``include_filesystem=True`` the backend root is restricted to
            only these paths plus ``/memories/`` so the agent cannot wander
            into arbitrary project files.  Defaults to ``["/skills/"]``.
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
                """Read content of a specific MCP resource (e.g., prom://system/backends, am://alerts/active)."""
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
            middleware: List[Any] = []
            if include_filesystem:
                from deepagents.middleware.filesystem import FilesystemMiddleware
                from deepagents.backends import FilesystemBackend
                from k8s_autopilot.utils.memory import get_project_root

                root = str(get_project_root())
                _allowed_paths = skill_paths or ["/skills/"]
                _paths_str = ", ".join(f"`{p}`" for p in _allowed_paths)
                middleware.append(
                    FilesystemMiddleware(
                        backend=FilesystemBackend(
                            root_dir=root,
                            virtual_mode=True,
                        ),
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

    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=RunnableLambda(_mcp_runnable).with_config({"run_name": name}),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_obs_subagent_specs(
    coordinator_model: Any = None,
) -> list[Any]:
    """Assemble sub-agent specs for the Observability deep agent.

    Returns Prometheus, Alertmanager, OpenTelemetry, and Loki sub-agents as
    JIT-connected MCP CompiledSubAgents.
    """
    from k8s_autopilot.core.agents.observability.middleware import (
        build_prometheus_hitl_middleware,
        build_alertmanager_hitl_middleware,
        build_opentelemetry_hitl_middleware,
        build_tempo_hitl_middleware,
    )

    coord_model = coordinator_model or ""

    return [
        # Prometheus sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            PROMETHEUS_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["prometheus-mcp-server"],
            mcp_resource_server_name="prometheus-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/prometheus/"],
            hitl_builder=build_prometheus_hitl_middleware,
        ),
        # Alertmanager sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            ALERTMANAGER_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["alertmanager-mcp-server"],
            mcp_resource_server_name="alertmanager-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/alertmanager/"],
            hitl_builder=build_alertmanager_hitl_middleware,
        ),
        # OpenTelemetry sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            OPENTELEMETRY_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["opentelemetry-mcp-server"],
            mcp_resource_server_name="opentelemetry-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/opentelemetry/"],
            hitl_builder=build_opentelemetry_hitl_middleware,
        ),
        # Loki sub-agent — read-only, no HITL middleware needed
        _build_mcp_subagent(
            LOKI_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["loki-mcp-server"],
            mcp_resource_server_name="loki-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/loki/"],
        ),
        # Tempo sub-agent — mostly read-only, 2 CRD tools need HITL
        _build_mcp_subagent(
            TEMPO_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["tempo-mcp-server"],
            mcp_resource_server_name="tempo-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/tempo/"],
            hitl_builder=build_tempo_hitl_middleware,
        ),
    ]
