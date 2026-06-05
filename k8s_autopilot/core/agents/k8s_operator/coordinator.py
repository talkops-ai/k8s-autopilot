"""
K8s Operator Deep Agent Coordinator.

Production-grade implementation of the deep agent pattern for Kubernetes
cluster operations. Wires backends, MCP tools, and subagents via the
``BaseDeepAgent`` abstract class.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

from langchain.tools import tool, ToolRuntime
from langgraph.store.memory import InMemoryStore

from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data

from k8s_autopilot.core.agents.types import BaseDeepAgent
from k8s_autopilot.core.state.k8s_operator_state import K8sOperatorContext
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.user_input_tool import (
    create_user_input_tool,
    create_chat_continue_tool,
)
from k8s_autopilot.utils.operations_context import create_log_k8s_operation_tool
from k8s_autopilot.core.agents.k8s_operator.subagents import get_k8s_subagent_specs
from k8s_autopilot.core.agents.k8s_operator.middleware import build_k8s_operator_middleware
from k8s_autopilot.utils.memory import K8sBackendMixin, get_project_root
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.domain_summary import extract_domain_summary

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("K8sOperatorCoordinator")

from k8s_autopilot.core.agents.k8s_operator.prompt_sections import (
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


class K8sOperatorCoordinator(BaseDeepAgent):
    """
    K8s Operator Deep Agent Coordinator.
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        *,
        mcp_server_filter: Optional[List[str]] = None,
    ) -> None:
        super().__init__(config=config)
        self._mcp_server_filter = mcp_server_filter

        logger.info("K8sOperatorCoordinator initialized")

    @property
    def name(self) -> str:
        return "k8s-operator-coordinator"

    @property
    def system_prompt(self) -> str:
        """Composed from modular prompt sections via PromptRegistry.

        The ``planning_mode`` section is overridden at runtime with the
        dynamic planning protocol from ``BaseDeepAgent.get_planning_prompt_section()``,
        which includes domain-specific task categories.

        Override individual sections at runtime:
            registry = create_coordinator_registry(identity="...")
            custom_prompt = registry.compose()
        """
        return compose_coordinator_prompt(
            planning_mode=self.get_planning_prompt_section(),
        )

    def get_task_categories(self) -> str:
        """K8s Operator domain-specific task categories."""
        return """\
- **Discovery**: Read-only cluster inspection (list pods, describe resources, get logs, events)
- **Validation**: Manifest validation, resource existence checks, context verification
- **Live Apply**: Resource CRUD (create, update, delete, scale, exec, run pod)
- **Health Check**: Pod readiness, rollout status, event monitoring
- **Rollback**: Undo operations, previous revision restore
- **Summary**: Generate walkthrough narrative from execution results"""

    @property
    def context_schema(self) -> type:
        return K8sOperatorContext

    def get_model(self) -> Any:
        return create_model(self._config.get_llm_deepagent_config())

    async def get_subagent_specs(self) -> List[Any]:
        return get_k8s_subagent_specs(coordinator_model=self.get_model())

    async def get_tools(self) -> List[Any]:
        user_input = create_user_input_tool()
        chat_continue = create_chat_continue_tool()
        log_operation = create_log_k8s_operation_tool()
        return [user_input, chat_continue, log_operation]

    def get_skill_paths(self) -> List[str]:
        return [
            "/skills/k8s-operator/kubernetes-cluster-ops",
        ]

    def get_memory_paths(self) -> List[str]:
        return [
            "/memories/k8s-operator/AGENTS.md",
            "/memories/k8s-operator/hitl-policies.md",
        ]

    def get_interrupt_config(self) -> Dict[str, Any]:
        return {}

    def make_backend(self) -> Any:
        return K8sBackendMixin.make_backend()

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
        if store.get(namespace, "k8s-operator/operations-log.md") is None:
            empty_log = (
                "# K8s Operations Journal\n\n"
                "Auto-generated log of operations performed in this session. "
                "Used by the coordinator to maintain context across conversation "
                "turns and after summarization.\n"
            )
            store.put(
                namespace,
                "k8s-operator/operations-log.md",
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

        logger.info("Building K8s Operator deep agent graph")

        self._store = self.build_store()
        checkpointer = self.build_checkpointer()
        tools = await self.get_tools()
        subagents = await self.get_subagent_specs()
        middleware = build_k8s_operator_middleware(config=self._config)

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
        state = supervisor_state or {}

        ctx: Dict[str, Any] = {
            "cluster_context":      os.getenv("K8S_CONTEXT", ""),
            "kubeconfig_path":      os.getenv("KUBECONFIG", ""),
            "default_namespace":    os.getenv("K8S_DEFAULT_NAMESPACE", "default"),
            "workspace_path":       os.getenv("HELM_WORKSPACE", "./workspace/helm-charts"),
            "read_only":            os.getenv("K8S_READ_ONLY", "false").lower() == "true",
            "disable_destructive":  os.getenv("K8S_DISABLE_DESTRUCTIVE", "false").lower() == "true",
        }

        if state.get("session_id"):
            ctx["session_id"] = state["session_id"]
        if state.get("task_id"):
            ctx["task_id"] = state["task_id"]

        caller_ctx: Dict[str, Any] = state.get("context") or {}
        if isinstance(caller_ctx, dict):
            ctx.update({k: v for k, v in caller_ctx.items() if v is not None and v != ""})

        for key in ("cluster_context", "kubeconfig_path", "workspace_path"):
            if ctx.get(key) == "":
                ctx.pop(key, None)

        # ── Cross-domain context ──────────────────────────────────────
        # If the supervisor routed here after another coordinator deferred
        # with "outside my scope", inject the structured prior context so
        # the K8s agent can use it instead of asking the user.
        cross_domain = state.get("cross_domain_context")
        if isinstance(cross_domain, dict) and cross_domain:
            ctx["cross_domain_context"] = cross_domain

        # Propagate accumulated domain summaries for the blackboard pattern
        domain_summaries = state.get("domain_summaries")
        if isinstance(domain_summaries, list) and domain_summaries:
            ctx["domain_summaries"] = domain_summaries

        return ctx

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
            "final_message": final_message or "K8s operator completed.",
            "status": "completed",
            "k8s_operator_output": {
                "messages": messages,
                "structured_response": state.get("structured_response"),
            },
            "domain_summary": extract_domain_summary(
                domain="k8s",
                final_message=final_message,
            ),
        }

        return output


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_k8s_operator_coordinator(
    config: Optional["Config"] = None,
    mcp_server_filter: Optional[List[str]] = None,
) -> K8sOperatorCoordinator:
    """
    Create a K8sOperatorCoordinator instance.

    Usage::

        from k8s_autopilot.core.agents.k8s_operator.coordinator import create_k8s_operator_coordinator
        coordinator = create_k8s_operator_coordinator(config)
    """
    return K8sOperatorCoordinator(config=config, mcp_server_filter=mcp_server_filter)
