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
# Static sub-agent dict specs (consumed by shared_subagent.build_mcp_subagent)
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

# The shared builder is imported from the central module to avoid 4x duplication.
# See shared_subagent.py for the full implementation.
from k8s_autopilot.core.agents.shared_subagent import build_mcp_subagent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_obs_subagent_specs(
    coordinator_model: Any = None,
) -> list[Any]:
    """Assemble sub-agent specs for the Observability deep agent.

    Returns Prometheus, Alertmanager, OpenTelemetry, Loki, and Tempo
    sub-agents as JIT-connected MCP CompiledSubAgents.

    Each sub-agent receives a ``CodeInterpreterMiddleware`` via
    ``extra_middleware_builders`` with a domain-specific read-only PTC
    allowlist.  This lets the agent batch MCP queries programmatically
    (e.g. loop over 10 services in a single ``eval`` call) instead of
    issuing 25+ sequential tool calls.

    Ref: https://docs.langchain.com/oss/python/deepagents/interpreters#enable-ptc
    """
    from k8s_autopilot.core.agents.observability.middleware import (
        build_prometheus_hitl_middleware,
        build_alertmanager_hitl_middleware,
        build_opentelemetry_hitl_middleware,
        build_tempo_hitl_middleware,
    )
    from k8s_autopilot.core.agents.shared_middleware import (
        make_subagent_interpreter_builder,
        PROMETHEUS_PTC_ALLOWLIST,
        ALERTMANAGER_PTC_ALLOWLIST,
        OPENTELEMETRY_PTC_ALLOWLIST,
        LOKI_PTC_ALLOWLIST,
        TEMPO_PTC_ALLOWLIST,
    )
    from k8s_autopilot.core.tools.kubectl_tools import create_kubectl_readonly_tool
    from k8s_autopilot.core.a2ui.obs_a2ui_tools import create_obs_a2ui_tools

    coord_model = coordinator_model or ""

    # A2UI visualization tool (shared by all subagents)
    a2ui_tools = create_obs_a2ui_tools()

    # Builder for A2UI Buffer Interceptor
    def make_a2ui_buffer_builder():
        def _builder():
            from k8s_autopilot.core.agents.observability.middleware import A2UIBufferMiddleware
            return A2UIBufferMiddleware()
        return _builder

    a2ui_builder = make_a2ui_buffer_builder()

    return [
        # Prometheus sub-agent — filesystem + skills + PTC interpreter
        build_mcp_subagent(
            PROMETHEUS_OPERATOR_SUBAGENT,
            server_filter=["prometheus-mcp-server"],
            mcp_resource_server_name="prometheus-mcp-server",
            include_filesystem=True,
            skill_paths=[
                "/skills/observability/prometheus/",
                "/skills/observability/response-formats/",
            ],
            hitl_builder=build_prometheus_hitl_middleware,
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=PROMETHEUS_PTC_ALLOWLIST,
                ),
                a2ui_builder,
            ],
            extra_tools=[create_kubectl_readonly_tool()] + a2ui_tools,
        ),
        # Alertmanager sub-agent — filesystem + skills + PTC interpreter
        build_mcp_subagent(
            ALERTMANAGER_OPERATOR_SUBAGENT,
            server_filter=["alertmanager-mcp-server"],
            mcp_resource_server_name="alertmanager-mcp-server",
            include_filesystem=True,
            skill_paths=[
                "/skills/observability/alertmanager/",
                "/skills/observability/response-formats/",
            ],
            hitl_builder=build_alertmanager_hitl_middleware,
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=ALERTMANAGER_PTC_ALLOWLIST,
                ),
                a2ui_builder,
            ],
            extra_tools=[create_kubectl_readonly_tool()] + a2ui_tools,
        ),
        # OpenTelemetry sub-agent — filesystem + skills + PTC interpreter
        build_mcp_subagent(
            OPENTELEMETRY_OPERATOR_SUBAGENT,
            server_filter=["opentelemetry-mcp-server"],
            mcp_resource_server_name="opentelemetry-mcp-server",
            include_filesystem=True,
            skill_paths=[
                "/skills/observability/opentelemetry/",
                "/skills/observability/response-formats/",
            ],
            hitl_builder=build_opentelemetry_hitl_middleware,
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=OPENTELEMETRY_PTC_ALLOWLIST,
                ),
                a2ui_builder,
            ],
            extra_tools=[create_kubectl_readonly_tool()] + a2ui_tools,
        ),
        # Loki sub-agent — read-only, no HITL, + PTC interpreter
        build_mcp_subagent(
            LOKI_OPERATOR_SUBAGENT,
            server_filter=["loki-mcp-server"],
            mcp_resource_server_name="loki-mcp-server",
            include_filesystem=True,
            skill_paths=[
                "/skills/observability/loki/",
                "/skills/observability/response-formats/",
            ],
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=LOKI_PTC_ALLOWLIST,
                ),
                a2ui_builder,
            ],
            extra_tools=a2ui_tools,
        ),
        # Tempo sub-agent — mostly read-only, 2 CRD tools need HITL + PTC interpreter
        build_mcp_subagent(
            TEMPO_OPERATOR_SUBAGENT,
            server_filter=["tempo-mcp-server"],
            mcp_resource_server_name="tempo-mcp-server",
            include_filesystem=True,
            skill_paths=[
                "/skills/observability/tempo/",
                "/skills/observability/response-formats/",
            ],
            hitl_builder=build_tempo_hitl_middleware,
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=TEMPO_PTC_ALLOWLIST,
                ),
                a2ui_builder,
            ],
            extra_tools=[create_kubectl_readonly_tool()] + a2ui_tools,
        ),
    ]

