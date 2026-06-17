"""
Observability Deep Agent Coordinator.

Production-grade implementation of the deep agent pattern for Prometheus
monitoring and Alertmanager alerting operations. Wires backends, MCP tools,
and subagents via the ``BaseDeepAgent`` abstract class.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

from langchain.tools import tool, ToolRuntime
from langgraph.store.memory import InMemoryStore

from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data

# Side-effect import: registers ProviderProfile + HarnessProfile for all
# supported providers BEFORE create_deep_agent constructs the model.
# Ref: https://docs.langchain.com/oss/python/deepagents/profiles
import k8s_autopilot.core.agents.profiles  # noqa: F401
from k8s_autopilot.core.agents.profiles import register_domain_profiles
register_domain_profiles("observability")

from k8s_autopilot.core.agents.types import BaseDeepAgent
from k8s_autopilot.core.state.observability_state import ObservabilityContext
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.user_input_tool import (
    create_user_input_tool,
    create_chat_continue_tool,
)
from k8s_autopilot.utils.operations_context import create_log_obs_operation_tool
from k8s_autopilot.utils.escalate_tool import create_escalate_to_supervisor_tool
from k8s_autopilot.core.agents.observability.subagents import get_obs_subagent_specs
from k8s_autopilot.core.agents.observability.middleware import build_obs_operator_middleware
from k8s_autopilot.utils.memory import K8sBackendMixin, get_project_root
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.domain_summary import extract_domain_summary
from k8s_autopilot.core.state.handoff_contracts import extract_handoff_from_text

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("ObservabilityCoordinator")

from k8s_autopilot.core.agents.observability.prompt_sections import (
    compose_coordinator_prompt,
    create_coordinator_registry,
)

# The coordinator prompt is composed from modular, testable prompt sections
# registered in prompt_sections.py.  Each XML block (<identity>, <scope>,
# <routing_rules>, etc.) is a standalone constant that can be overridden,
# tested, or measured for token cost independently.
#
# To customise the prompt at runtime, use create_coordinator_registry()
# with overrides:
#     registry = create_coordinator_registry(identity="<identity>Custom</identity>")
#     prompt = registry.compose()
#
# See prompt_sections.py for the full list of sections and their content.
OBS_COORDINATOR_PROMPT = compose_coordinator_prompt()

class ObservabilityCoordinator(BaseDeepAgent):
    """
    Observability Deep Agent Coordinator.

    Orchestrates Prometheus monitoring, Alertmanager alerting, OpenTelemetry pipeline,
    Loki log, and Tempo distributed tracing operations via five specialized sub-agents
    connected to their respective MCP servers.
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        *,
        mcp_server_filter: Optional[List[str]] = None,
    ) -> None:
        super().__init__(config=config)
        self._mcp_server_filter = mcp_server_filter

        logger.info("ObservabilityCoordinator initialized")

    @property
    def name(self) -> str:
        return "observability-coordinator"

    @property
    def system_prompt(self) -> str:
        """Composed from modular prompt sections via PromptRegistry.

        Override individual sections at runtime:
            registry = create_coordinator_registry(identity="...")
            custom_prompt = registry.compose()
        """
        return OBS_COORDINATOR_PROMPT

    def get_task_categories(self) -> str:
        """Observability domain-specific task categories."""
        return """\
- **Discovery**: Read-only metric queries, alert listing, exporter status, target health
- **Configuration**: ServiceMonitor creation, rule authoring, probe setup
- **Validation**: PromQL expression testing, rule simulation, integration testing
- **Live Apply**: Exporter install/uninstall, silence creation/expiry, rule upsert, probe apply
- **Health Check**: up metric verification, probe_success checks, alert fire verification
- **Monitoring**: Cross-signal validation, temporal correlation, RCA investigation
- **Summary**: Generate walkthrough narrative from execution results"""

    @property
    def context_schema(self) -> type:
        return ObservabilityContext

    def get_model(self) -> Any:
        return create_model(self._config.get_llm_deepagent_config())

    async def get_subagent_specs(self) -> List[Any]:
        return get_obs_subagent_specs(coordinator_model=self.get_model())

    async def get_tools(self) -> List[Any]:
        user_input = create_user_input_tool()
        chat_continue = create_chat_continue_tool()
        log_operation = create_log_obs_operation_tool()
        escalate = create_escalate_to_supervisor_tool()
        return [user_input, chat_continue, log_operation, escalate]

    def get_skill_paths(self) -> List[str]:
        return [
            "/skills/observability/prometheus",
            "/skills/observability/alertmanager",
            "/skills/observability/opentelemetry",
            "/skills/observability/loki",
            "/skills/observability/tempo",
            "/skills/observability/response-formats",
        ]

    def get_memory_paths(self) -> List[str]:
        return [
            "/memories/observability/AGENTS.md",
            "/memories/observability/hitl-policies.md",
            "/memories/observability/knowledge/",
        ]

    def get_interrupt_config(self) -> Dict[str, Any]:
        return {}

    def make_backend(self) -> Any:
        from deepagents.backends import (
            CompositeBackend,
            FilesystemBackend,
            StateBackend,
            StoreBackend,
        )
        from k8s_autopilot.utils.memory import get_project_root

        root = get_project_root()
        default = FilesystemBackend(
            root_dir=str(root),
            virtual_mode=True,
        )

        _org = os.getenv("ORG_NAME", "default_org")

        return CompositeBackend(
            default=default,
            routes={
                "/memories/": StoreBackend(
                    namespace=lambda _rt: (_org,),
                ),
                "/shared/": StoreBackend(
                    namespace=lambda _rt: ("shared",),
                ),
                "/skills/": StateBackend(),
            },
        )

    def build_store(self) -> Any:
        store = InMemoryStore()
        project_root = get_project_root()
        memory_dir = project_root / "memory"

        namespace = ("default_org",)

        if memory_dir.exists():
            for path in memory_dir.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    key = path.relative_to(memory_dir).as_posix()
                    try:
                        store.put(
                            namespace,
                            key,
                            dict(create_file_data(path.read_text(encoding="utf-8"))),
                        )
                    except UnicodeDecodeError:
                        pass

        # Pre-seed operations-log if not populated to prevent "File not found" read errors
        if store.get(namespace, "observability/operations-log.md") is None:
            empty_log = (
                "# Observability Operations Journal\n\n"
                "Auto-generated log of operations performed in this session. "
                "Used by the coordinator to maintain context across "
                "conversation turns and after summarization.\n"
            )
            store.put(
                namespace,
                "observability/operations-log.md",
                dict(create_file_data(empty_log)),
            )

        return store

    def build_checkpointer(self) -> Any:
        """Return None to inherit the parent supervisor's checkpointer.

        Per-invocation mode (checkpointer=None) is the recommended pattern
        for subagents invoked as tools.  The child inherits the parent's
        checkpointer via the config passed to ainvoke(), enabling native
        interrupt()/resume support without manual bridging.

        Reference: LangGraph docs — Subgraph persistence / Per-invocation.
        """
        return None

    async def build_agent(self) -> Any:
        if getattr(self, "_agent", None):
            return self._agent

        logger.info("Building Observability deep agent graph")

        self._store = self.build_store()
        checkpointer = self.build_checkpointer()
        tools = await self.get_tools()
        subagents = await self.get_subagent_specs()
        middleware = build_obs_operator_middleware(
            config=self._config,
            model=self.get_model(),
            backend=self.make_backend(),
        )

        self._agent = create_deep_agent(
            model=self.get_model(),
            name=self.name,
            system_prompt=self.system_prompt,
            tools=tools,
            subagents=subagents,
            skills=self.get_skill_paths(),
            memory=self.get_memory_paths(),
            backend=self.make_backend(),
            store=self._store,
            checkpointer=checkpointer,
            interrupt_on=self.get_interrupt_config(),
            context_schema=self.context_schema,
            middleware=middleware,
        )
        return self._agent

    def seed_files(
        self,
        skills_dir: Optional[Any] = None,
        memory_dir: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return K8sBackendMixin.seed_files(
            skill_paths=self.get_skill_paths(),
            memory_paths=self.get_memory_paths(),
        )

    def input_transform(self, send_payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = send_payload.get("messages", [])
        files = self.seed_files()
        transformed: Dict[str, Any] = {
            "messages": messages,
        }
        if files:
            transformed["files"] = files
        return transformed

    def build_context(
        self,
        supervisor_state: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Build context dict with selective injection (context engineering).

        Base context (always injected): connection URLs, backend IDs, namespace.
        Investigation context (only when SRE fields present): service, env,
        tenant, time window, incident ID.
        Cross-domain context (only when routed from another coordinator):
        prior findings, domain summaries.
        """
        state = supervisor_state or {}

        # ── Base context — always injected ─────────────────────────────
        ctx: Dict[str, Any] = self._build_base_context(state)

        # ── SRE investigation context — only when relevant ────────────
        # Propagate investigation-scoped fields from supervisor state so
        # subagents can auto-filter queries by service/env/incident.
        if self._is_investigation_mode(state):
            self._inject_investigation_context(ctx, state)

        # ── Cross-domain context — only when routed from another coordinator
        self._inject_cross_domain_context(ctx, state)

        # ── Caller context — merge any additional context from supervisor
        caller_ctx: Dict[str, Any] = state.get("context") or {}
        if isinstance(caller_ctx, dict):
            ctx.update({k: v for k, v in caller_ctx.items() if v is not None and v != ""})

        # Clean up empty connection strings
        for key in ("prometheus_url", "alertmanager_url", "cluster_context", "kubeconfig_path"):
            if ctx.get(key) == "":
                ctx.pop(key, None)

        return ctx

    @staticmethod
    def _build_base_context(state: Dict[str, Any]) -> Dict[str, Any]:
        """Build the base context that every subagent needs."""
        ctx: Dict[str, Any] = {
            "prometheus_url":       os.getenv("PROMETHEUS_BASE_URL", os.getenv("PROMETHEUS_URL", "")),
            "alertmanager_url":     os.getenv("ALERTMANAGER_BASE_URL", ""),
            "default_backend_id":   os.getenv("OBS_DEFAULT_BACKEND_ID", "default"),
            "cluster_context":      os.getenv("K8S_CONTEXT", ""),
            "kubeconfig_path":      os.getenv("KUBECONFIG", ""),
            "default_namespace":    os.getenv("K8S_DEFAULT_NAMESPACE", "default"),
        }

        if state.get("session_id"):
            ctx["session_id"] = state["session_id"]
        if state.get("task_id"):
            ctx["task_id"] = state["task_id"]

        return ctx

    @staticmethod
    def _is_investigation_mode(state: Dict[str, Any]) -> bool:
        """Check if the request contains SRE investigation fields."""
        _sre_fields = (
            "service_name", "environment", "tenant_id",
            "time_window", "incident_id",
        )
        return any(state.get(field) for field in _sre_fields)

    @staticmethod
    def _inject_investigation_context(
        ctx: Dict[str, Any], state: Dict[str, Any],
    ) -> None:
        """Inject SRE investigation-scoped fields into context."""
        _sre_fields = (
            "service_name", "environment", "tenant_id",
            "time_window", "incident_id", "user_id",
        )
        for field in _sre_fields:
            val = state.get(field)
            if val:
                ctx[field] = val

        # Merge additional_labels if provided by supervisor
        extra_labels = state.get("additional_labels")
        if isinstance(extra_labels, dict) and extra_labels:
            ctx["additional_labels"] = extra_labels

    @staticmethod
    def _inject_cross_domain_context(
        ctx: Dict[str, Any], state: Dict[str, Any],
    ) -> None:
        """Inject cross-domain routing context when applicable."""
        # If the supervisor routed here after another coordinator deferred
        # with "outside my scope", inject the structured prior context so
        # the observability agent can use it instead of asking the user.
        cross_domain = state.get("cross_domain_context")
        if isinstance(cross_domain, dict) and cross_domain:
            ctx["cross_domain_context"] = cross_domain

        # Propagate accumulated domain summaries for the blackboard pattern
        domain_summaries = state.get("domain_summaries")
        if isinstance(domain_summaries, list) and domain_summaries:
            ctx["domain_summaries"] = domain_summaries

    def output_transform(
        self,
        agent_state: Dict[str, Any],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        state: Dict[str, Any] = agent_state
        if not isinstance(agent_state, dict) and hasattr(agent_state, "model_dump"):
            state = agent_state.model_dump()

        final_message: Optional[str] = None
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            final_message = getattr(last_msg, "content", None) or (
                last_msg.get("content") if isinstance(last_msg, dict) else None
            )

        output: Dict[str, Any] = {
            "final_message": final_message or "Observability operator completed.",
            "status": "completed",
            "observability_output": {
                "messages": messages,
                "structured_response": state.get("structured_response"),
            },
            # ── Domain summary for supervisor blackboard ──────────────
            # Compact structured summary that the supervisor accumulates
            # and passes to downstream coordinators for cross-domain
            # awareness.  Keeps only distilled findings, not raw data.
            "domain_summary": extract_domain_summary(
                domain="observability",
                final_message=final_message,
            ),
        }
        return output

# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_observability_coordinator(
    config: Optional["Config"] = None,
    mcp_server_filter: Optional[List[str]] = None,
) -> ObservabilityCoordinator:
    """
    Create an ObservabilityCoordinator instance.

    Usage::

        from k8s_autopilot.core.agents.observability.coordinator import create_observability_coordinator
        coordinator = create_observability_coordinator(config)
    """
    return ObservabilityCoordinator(config=config, mcp_server_filter=mcp_server_filter)
