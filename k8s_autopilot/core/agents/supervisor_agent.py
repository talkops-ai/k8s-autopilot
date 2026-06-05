"""K8s Autopilot Supervisor Agent — pure router delegating to coordinators."""

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated, Any, cast

from langchain.agents import create_agent
from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from k8s_autopilot.config.config import Config
from k8s_autopilot.core.state.base import (
    MainSupervisorState,
    SupervisorWorkflowState,
)
from k8s_autopilot.utils.exceptions import AgentExecutionError
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.logger import AgentLogger, log_async, log_sync
from k8s_autopilot.core.a2ui.dynamic_schema import create_generate_a2ui_tool

from .supervisor_middleware import build_supervisor_middleware
from .types import AgentResponse, BaseAgent, BaseDeepAgent, BaseSubgraphAgent

logger = AgentLogger("SupervisorAgent")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SUPERVISOR_PROMPT = """\
You are the K8s Autopilot Supervisor — a pure router that delegates ALL \
Kubernetes infrastructure requests to the appropriate coordinator.

**Routing table — match the request to the correct tool:**

| Request type | Tool |
|---|---|
| Create / generate / update Helm charts | ``transfer_to_helm_operator`` |
| K8s cluster ops (pods, scaling, exec, events) | ``transfer_to_k8s_operator`` |
| ArgoCD projects / repos / apps / sync / debug | ``transfer_to_app_operator`` |
| Argo Rollouts canary / blue-green / analysis / list | ``transfer_to_app_operator`` |
| Traefik routing, middleware, traffic mgmt | ``transfer_to_app_operator`` |
| Prometheus metrics, queries, exporters, rules, TSDB | ``transfer_to_observability_operator`` |
| Alertmanager alerts, silences, routing, triage | ``transfer_to_observability_operator`` |
| Out-of-scope / greetings / clarification | ``request_human_feedback`` |

**VALID REQUEST EXAMPLES:**
- "Create a Helm chart for nginx" → transfer_to_helm_operator
- "Update my Helm chart templates" → transfer_to_helm_operator
- "List pods in production namespace" → transfer_to_k8s_operator
- "Scale deployment nginx to 5 replicas" → transfer_to_k8s_operator
- "Delete the stuck pod" → transfer_to_k8s_operator
- "Check cluster health" → transfer_to_k8s_operator
- "Run a debug pod with busybox" → transfer_to_k8s_operator
- "Create an ArgoCD project" → transfer_to_app_operator
- "Sync my-app ArgoCD application" → transfer_to_app_operator
- "Set 80/20 canary split" → transfer_to_app_operator
- "Show me the rollout list" → transfer_to_app_operator
- "Configure Traefik weighted routing" → transfer_to_app_operator
- "What alerts are firing?" → transfer_to_observability_operator
- "Query CPU metrics" → transfer_to_observability_operator
- "Silence the noisy alerts" → transfer_to_observability_operator
- "Install postgres exporter" → transfer_to_observability_operator
- "Check cardinality" → transfer_to_observability_operator
- "Create alerting rule for high error rate" → transfer_to_observability_operator
- "Who gets paged for critical alerts?" → transfer_to_observability_operator

**OUT-OF-SCOPE REQUEST HANDLING:**
If a request is NOT related to Helm/ArgoCD/K8s/Traefik/Prometheus/Alertmanager:
1. **CRITICAL: Use request_human_feedback** - no direct text
2. **Create dynamic, contextual messages** for the user
3. **NEVER output text without calling request_human_feedback**

Available tools:
- transfer_to_helm_operator: Helm chart generation/update
- transfer_to_k8s_operator: K8s cluster ops (pods, scale, exec)
- transfer_to_app_operator: ArgoCD, Argo Rollouts, Traefik
- transfer_to_observability_operator: Prometheus monitoring, Alertmanager alerting
- request_human_feedback: Human feedback or clarification

**TASK DESCRIPTION CRAFTING (for transfer_to_* tools):**
Translate the user's intent into a clear DevOps-aware task description.
If the user uses non-technical language, map it to the correct domain:

| User says | Translate to | Tool |
|---|---|---|
| "deploy" / "ship" / "release" | Create or sync ArgoCD Application | transfer_to_app_operator |
| "rollback" / "undo" / "revert" | ArgoCD rollback or Rollout abort | transfer_to_app_operator |
| "zero downtime" / "gradual" | Argo Rollouts canary/blue-green | transfer_to_app_operator |
| "split traffic" / "A/B test" | Traefik weighted routing | transfer_to_app_operator |
| "scale up" / "more capacity" | K8s scaling or HPA | transfer_to_k8s_operator |
| "check" / "status" / "health" | Read-only diagnostics | appropriate operator |
| "what's firing" / "alerts" / "on-call" | Alert triage | transfer_to_observability_operator |
| "silence" / "mute" / "suppress" | Create silence | transfer_to_observability_operator |
| "metrics" / "query" / "PromQL" | Prometheus query | transfer_to_observability_operator |
| "exporter" / "monitor postgres" | Exporter lifecycle | transfer_to_observability_operator |
| "alerting rule" / "notify when" | Rule authoring | transfer_to_observability_operator |
| "cardinality" / "TSDB" / "storage" | TSDB FinOps | transfer_to_observability_operator |

Parse the request, extract intent, create a clear description.
Do NOT pass raw user messages verbatim.

**CROSS-DOMAIN RE-ROUTING (CRITICAL):**
If a coordinator returns a message containing "outside my scope" (typically formatted with "User Request:" and "Context:"):
→ Do NOT summarize the context to the user. Do NOT complete the workflow.
→ Read the "User Request" section to determine the correct coordinator tool.
→ Call the correct coordinator tool immediately. Do NOT ask the user for permission.
→ Pass both the "User Request" and "Context" in the task parameter to the new coordinator.
→ **PREFIX the task with cross-domain context**: \
  `[CROSS-DOMAIN] Source: {source_domain}. Prior findings: {context_summary}. User Request: {request}`
Example: If observability coordinator returns "User Request: Check pod status for checkout":
  `[CROSS-DOMAIN] Source: observability. Prior findings: 5 critical alerts for checkout service, silence created. User Request: Check pod status for checkout service`
Example: If k8s coordinator returns "User Request: What alerts are firing?":
  `[CROSS-DOMAIN] Source: k8s_operator. Prior findings: checkout pods restarting (CrashLoopBackOff). User Request: Check if any alerts are firing for checkout`

**ERROR RECOVERY:**
If a coordinator returns an error (e.g., "encountered an error", MCP unavailable):
→ Use `request_human_feedback` to inform the user what happened and suggest alternatives.
→ Do NOT retry the same coordinator in a loop. Max 1 retry, then report to user.

**CRITICAL RULES:**
- Always call tools immediately, don't describe what you will do
- Do NOT do chart generation/validation/K8s ops yourself
- You are a ROUTER, not a CREATOR
- When a coordinator defers to another domain, YOU must re-route — never leave the user stranded
""".strip()


# ---------------------------------------------------------------------------
# Cross-domain handoff detection
# ---------------------------------------------------------------------------

_HANDOFF_PATTERNS: tuple[str, ...] = (
    "use the k8s operator",
    "use the kubernetes operator",
    "use the kubernetes assistant",
    "use the helm operator",
    "use the app operator",
    "use the observability operator",
    "use the prometheus operator",
    "use the alertmanager operator",
    "use the k8s-operator",
    "use the k8s cluster",
    "k8s operator to inspect",
    "k8s operator can",
    "kubernetes operator can",
    "kubernetes assistant can",
    "helm operator can",
    "app operator can",
    "observability operator can",
    "outside my scope",
    "outside of my scope",
    "beyond my scope",
    "not within my capabilities",
    "different domain",
    "another operator",
    "so the kubernetes operator can take over",
    "so the k8s operator can take over",
    "so the helm operator can take over",
    "so the app operator can take over",
    "so the observability operator can take over",
    "returning to coordinator for re-routing",
)


def _detect_cross_domain_handoff(content: Any) -> bool:
    """Return True if content contains a cross-domain handoff signal."""
    text = _extract_content_text(content)
    return any(p in text.lower() for p in _HANDOFF_PATTERNS)


def _extract_handoff_context(
    message: str,
    source_tool: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse structured handoff context from a coordinator out-of-scope message."""
    source_domain = source_tool.replace("transfer_to_", "").replace("_operator", "")
    context: dict[str, Any] = {
        "source_domain": source_domain,
        "source_tool": source_tool,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    if "User Request:" in message:
        parts = message.split("User Request:", 1)
        request_part = parts[1]
        context["user_request"] = (
            request_part.split("Context:")[0].strip()
            if "Context:" in request_part
            else request_part.strip()
        )

    if "Context:" in message:
        context["prior_context"] = message.split("Context:", 1)[1].strip()

    if isinstance(payload, dict) and payload.get("domain_summary"):
        context["domain_summary"] = payload["domain_summary"]

    return context


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

# Compiled regex patterns for stripping internal repr objects
_REPR_PATTERNS = [
    re.compile(p, re.DOTALL)
    for p in (
        r"Command\(update=\{.*?\}\)",
        r"ToolMessage\(content=.*?\)",
        r"AIMessage\(content=.*?\)",
        r"HumanMessage\(content=.*?\)",
        r"\[ToolMessage\(.*?\)\]",
    )
]
_WHITESPACE_RE = re.compile(r"\s+")


def _extract_content_text(content: Any) -> str:
    """Extract plain text from content (handles string and list-of-blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content) if content else ""


def _extract_reasoning_text(msg_chunk: Any) -> str:
    """Extract reasoning/thinking from an AIMessageChunk (provider-agnostic).

    Covers Gemini (thinking/thought blocks), OpenAI o1/o3 (reasoning_content
    in additional_kwargs), and Anthropic (thinking blocks).
    """
    parts: list[str] = []

    # 1. Content list — thinking/thought/reasoning blocks (Gemini, Anthropic)
    raw_content = getattr(msg_chunk, "content", None)
    if isinstance(raw_content, list):
        _THINKING_TYPES = {"thinking", "thought", "reasoning"}
        _THINKING_KEYS = ("thinking", "thought", "reasoning", "text")
        for block in raw_content:
            if isinstance(block, dict) and block.get("type", "") in _THINKING_TYPES:
                text = next((block[k] for k in _THINKING_KEYS if block.get(k)), "")
                if text:
                    parts.append(str(text))

    # 2. additional_kwargs — OpenAI o1/o3 reasoning_content, Gemini thinking
    if not parts:
        kwargs = getattr(msg_chunk, "additional_kwargs", None) or {}
        for key in ("thinking", "thought", "reasoning_content"):
            val = kwargs.get(key)
            if isinstance(val, str) and val:
                parts.append(val)

    return "".join(parts)


def _humanize_tool_name(raw_name: str) -> str:
    """Convert snake_case tool name to Title Case display name."""
    if not raw_name:
        return "Tool"

    name = raw_name
    for prefix in ("transfer_to_", "kubernetes_", "kubectl_", "k8s_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    return name.replace("_", " ").strip().title() or raw_name.title()


def _format_tool_args(args: Any, max_len: int = 200) -> str:
    """Format tool args into a compact display string."""
    if not args:
        return ""

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return args[:max_len]

    if not isinstance(args, dict):
        return str(args)[:max_len]

    parts: list[str] = []
    for key, val in args.items():
        if key == "tool_call_id":
            continue
        val_str = str(val) if not isinstance(val, str) else val
        if len(val_str) > 80:
            val_str = val_str[:77] + "..."
        parts.append(f'{key}: "{val_str}"')

    result = ", ".join(parts)
    return result[:max_len - 3] + "..." if len(result) > max_len else result


def _sanitize_result_snippet(raw: Any, max_len: int = 200) -> str:
    """Clean up tool result for the thinking stream."""
    text = str(raw or "").strip()
    if not text:
        return ""

    for pattern in _REPR_PATTERNS:
        text = pattern.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()

    if not text:
        return ""
    return text[:max_len - 1] + "…" if len(text) > max_len else text


def _extract_interrupt_tool_name(interrupts: tuple) -> str:
    """Extract the tool name from an interrupt payload."""
    if not interrupts:
        return ""

    first = interrupts[0]
    value = getattr(first, "value", first)
    if not isinstance(value, dict):
        return ""

    # pending_feedback_requests (request_human_feedback)
    feedback = value.get("pending_feedback_requests", {})
    if isinstance(feedback, dict) and feedback.get("tool_name"):
        return str(feedback["tool_name"])

    # action_requests (HITL middleware)
    action_reqs = value.get("action_requests", [])
    if isinstance(action_reqs, list) and action_reqs:
        action = action_reqs[0].get("action", {}) if isinstance(action_reqs[0], dict) else {}
        if isinstance(action, dict) and action.get("tool"):
            return str(action["tool"])

    # Custom interrupt with "type" key
    custom_type = value.get("type", "")
    return custom_type if custom_type and custom_type != "generic" else ""


def _extract_tc_fields(tc: Any) -> tuple[str | None, Any]:
    """Extract (name, args) from a tool call object (dict or object)."""
    if isinstance(tc, dict):
        return tc.get("name"), tc.get("args") or tc.get("input")
    return getattr(tc, "name", None), getattr(tc, "args", None)


# ---------------------------------------------------------------------------
# Subgraph event unpacking helpers
# ---------------------------------------------------------------------------

def _unpack_subgraph_event(event: Any) -> tuple[str, Any]:
    """Unpack a subgraphs=True event → (source_label, raw_event).

    When stream_mode is a tuple/list and subgraphs=True, events arrive as:
        (namespace_tuple, stream_mode_tag, payload)   — 3-tuple
    When stream_mode is a single string and subgraphs=True:
        (namespace_tuple, payload)                     — 2-tuple
    Reference: LangGraph Pregel.astream() docstring.

    Returns ("", None) if the event shape is unrecognized.
    """
    if not isinstance(event, tuple):
        return "", None

    def _ns_label(ns: Any) -> str:
        """Extract a human-readable label from a namespace tuple element.

        LangGraph namespace elements use ``node_name:task_id`` format
        (e.g. ``"tools:abc123"``, ``"agent:def456"``).  Strip the
        task_id suffix to get the node/source name.
        """
        if not isinstance(ns, tuple) or not ns:
            return "coordinator"
        last = ns[-1]
        # Strip :task_id suffix if present
        return last.split(":")[0] if ":" in last else last

    if len(event) == 3:
        # (namespace_tuple, stream_mode_tag, payload)
        ns, mode_tag, payload = event
        label = _ns_label(ns)
        if mode_tag == "values":
            return label, payload       # dict → captured as final_state
        if mode_tag == "messages":
            return label, payload       # (msg_chunk, metadata) tuple
        return label, payload

    if len(event) == 2:
        first, second = event
        if isinstance(first, tuple):
            # (namespace_tuple, raw_event)
            label = _ns_label(first)
            return label, second
        # Fallback: no subgraph wrapping
        return "coordinator", event

    return "", None


def _emit_message_deltas(
    runtime: Any,
    msg_chunk: Any,
    metadata: Any,
    tool_name: str,
    source_label: str,
) -> None:
    """Emit output deltas for a single message chunk to the parent stream.

    Handles ToolMessage results, reasoning tokens, text tokens,
    tool_call_chunks, and full tool_calls — all provider-agnostic.
    """
    node = (
        metadata.get("langgraph_node", "subagent")
        if isinstance(metadata, dict)
        else "subagent"
    )
    raw_content = getattr(msg_chunk, "content", "")
    content = _extract_content_text(raw_content)

    # ToolMessage → tool_finished
    if isinstance(msg_chunk, ToolMessage):
        if content:
            runtime.emit_output_delta({
                "type": "tool_finished",
                "tool_name": getattr(msg_chunk, "name", None) or "tool",
                "output": content[:500],
                "node": node,
                "source": tool_name,
                "subagent": source_label,
            })
        return

    # Reasoning tokens (provider-agnostic)
    reasoning = _extract_reasoning_text(msg_chunk)
    if reasoning:
        runtime.emit_output_delta({
            "type": "token",
            "text": reasoning,
            "delta_type": "reasoning-delta",
            "node": node,
            "source": tool_name,
            "subagent": source_label,
        })

    # Text tokens
    if content:
        runtime.emit_output_delta({
            "type": "token",
            "text": content,
            "delta_type": "text-delta",
            "node": node,
            "source": tool_name,
            "subagent": source_label,
        })

    # Tool call chunks (streaming partial tool calls)
    for tc_chunk in getattr(msg_chunk, "tool_call_chunks", None) or []:
        tc_name, tc_args = _extract_tc_fields(tc_chunk)
        if tc_name:
            runtime.emit_output_delta({
                "type": "tool_call",
                "tool_name": tc_name,
                "tool_input": tc_args,
                "node": node,
                "source": tool_name,
                "subagent": source_label,
            })

    # Full tool calls (finalized) — only emit "tool_started" if no
    # streaming chunks were already sent for the same call.
    # Per LangChain docs: tool_call_chunks = progressive arg rendering,
    # tool_calls = finalized lifecycle. Emitting both creates duplicate cards.
    has_streaming_chunks = bool(getattr(msg_chunk, "tool_call_chunks", None))
    for tc in getattr(msg_chunk, "tool_calls", None) or []:
        if has_streaming_chunks:
            continue  # Already emitted as "tool_call" via tool_call_chunks
        tc_name, tc_args = _extract_tc_fields(tc)
        if tc_name:
            runtime.emit_output_delta({
                "type": "tool_started",
                "tool_name": tc_name,
                "tool_input": tc_args,
                "node": node,
                "source": tool_name,
                "subagent": source_label,
            })


# ---------------------------------------------------------------------------
# Coordinator tool spec table
# ---------------------------------------------------------------------------

_COORDINATOR_SPECS: list[tuple[str, str, str, str, str]] = [
    (
        "transfer_to_helm_operator",
        "helm-operator-coordinator",
        "helm_operator_output",
        "helm_operator",
        "Delegate Helm chart generation and updates.",
    ),
    (
        "transfer_to_k8s_operator",
        "k8s-operator-coordinator",
        "k8s_operator_output",
        "k8s_operator",
        "Delegate K8s cluster operations (pods, scaling, exec, events, diagnostics).",
    ),
    (
        "transfer_to_app_operator",
        "app-operator-coordinator",
        "app_operator_output",
        "app_operator",
        "Delegate app lifecycle operations (ArgoCD, Argo Rollouts, Traefik).",
    ),
    (
        "transfer_to_observability_operator",
        "observability-coordinator",
        "observability_output",
        "observability_operator",
        "Delegate observability operations (Prometheus, Alertmanager).",
    ),
]


# ---------------------------------------------------------------------------
# SupervisorAgent
# ---------------------------------------------------------------------------

class k8sAutopilotSupervisorAgent(BaseAgent):  # noqa: N801
    """Supervisor agent — routes requests to domain coordinators via tool wrappers."""

    @log_sync
    def __init__(
        self,
        agents: list[BaseSubgraphAgent] | None = None,
        config: Config | None = None,
        custom_config: dict[str, Any] | None = None,
        prompt_template: str | None = None,
        name: str = "supervisor-agent",
        *,
        coordinator: BaseDeepAgent | None = None,
        coordinators: list[BaseDeepAgent] | None = None,
    ):
        self.config_instance = config or Config(custom_config or {})
        self._name = name

        try:
            from k8s_autopilot.core.hitl import get_checkpointer  # noqa: PLC0415
            self.memory = get_checkpointer(config=self.config_instance, prefer_postgres=True)
        except Exception:  # noqa: BLE001
            self.memory = MemorySaver()

        self.model = create_model(self.config_instance.get_llm_config())

        # Coordinator(s) — multi-coordinator is preferred
        self.agents: dict[str, Any] = {}
        self._coordinator: BaseDeepAgent | None = None

        if coordinators:
            for coord in coordinators:
                self.agents[coord.name] = coord
            self._coordinator = coordinators[0]
        elif coordinator is not None:
            self._coordinator = coordinator
            self.agents[coordinator.name] = coordinator
        else:
            for agent in (agents or []):
                if hasattr(agent, "memory"):
                    agent.memory = self.memory
                self.agents[agent.name] = agent

        self.prompt_template = prompt_template or SUPERVISOR_PROMPT
        self._graph = self._build_supervisor_graph()

        mode = (
            "coordinators" if coordinators
            else ("coordinator" if self._coordinator else "legacy")
        )
        logger.info(
            "Supervisor agent initialized",
            extra={"mode": mode, "agent_count": len(self.agents), "agent_names": list(self.agents.keys())},
        )

    @property
    def name(self) -> str:
        return self._name

    # ── Graph builder ─────────────────────────────────────────────────

    def _build_supervisor_graph(self) -> CompiledStateGraph:
        """Build supervisor using create_agent() with tool wrappers."""
        if self._coordinator is None:
            msg = "No coordinator configured for supervisor"
            raise RuntimeError(msg)

        agent_tools = self._create_agent_tools(self._coordinator)
        feedback_tool = self._make_request_human_feedback_tool()
        a2ui_tool = create_generate_a2ui_tool(config=self.config_instance.get_llm_config())
        all_tools = [*agent_tools, feedback_tool, a2ui_tool]

        logger.info(
            "Creating supervisor with create_agent()",
            extra={"tool_names": [t.name for t in all_tools]},
        )

        return create_agent(
            model=self.model,
            tools=all_tools,
            system_prompt=self.prompt_template,
            state_schema=MainSupervisorState,  # type: ignore[arg-type]
            checkpointer=cast("MemorySaver", self.memory),
            middleware=build_supervisor_middleware(self.config_instance),
        )

    # ── Tool wrappers ─────────────────────────────────────────────────

    @staticmethod
    def _make_coordinator_tool(
        coordinator: BaseDeepAgent,
        tool_name: str,
        tool_doc: str,
        output_key: str,
        phase_name: str,
    ) -> Any:
        """Create a @tool wrapper that delegates to a BaseDeepAgent coordinator."""

        @tool(tool_name)  # type: ignore[call-overload]
        async def _coordinator_tool(
            task_description: str,
            runtime: ToolRuntime[None, MainSupervisorState],
            tool_call_id: Annotated[str, InjectedToolCallId],
            config: RunnableConfig,
        ) -> Command:
            """Delegate to the deep agent coordinator.

            task_description: intent-based summary from user request.
            """
            logger.info(
                f"{tool_name} invoked",
                extra={
                    "task_description": task_description[:200],
                    "session_id": runtime.state.get("session_id"),
                    "task_id": runtime.state.get("task_id"),
                },
            )

            # Lazy-init the deep agent graph
            if not coordinator._is_initialized:
                logger.info(f"Building {tool_name} deep agent graph lazily")  # noqa: G004
                coordinator._deep_agent_graph = await coordinator.build_agent()
                coordinator._is_initialized = True

            deep_agent_graph = coordinator._deep_agent_graph
            if deep_agent_graph is None:
                msg = f"{tool_name}: deep agent graph not initialized"
                raise AgentExecutionError(msg)

            # Build input
            send_payload: dict[str, Any] = dict(runtime.state)
            send_payload["messages"] = [HumanMessage(content=task_description)]
            send_payload["user_query"] = task_description
            child_input = coordinator.input_transform(send_payload)

            # Build config
            child_config: dict[str, Any] = {k: v for k, v in config.items() if k != "store"}

            # Store bridging
            child_store = getattr(deep_agent_graph, "store", None)
            if child_store is None:
                bound = getattr(deep_agent_graph, "bound", None)
                if bound is not None:
                    child_store = getattr(bound, "store", None)

            configurable = dict(config.get("configurable", {}))
            runtime_obj = configurable.get("__pregel_runtime")
            if runtime_obj is not None and hasattr(runtime_obj, "override") and child_store is not None:
                configurable["__pregel_runtime"] = runtime_obj.override(store=child_store)

            child_config["configurable"] = {
                **configurable,
                "thread_id": f"{runtime.state.get('session_id', 'default')}:{tool_name}",
                "context": coordinator.build_context(supervisor_state=dict(runtime.state)),
            }
            child_config["recursion_limit"] = 250

            # ── Stream child graph ────────────────────────────────────
            try:
                final_state = None

                from langgraph.pregel._tools import _tool_call_writer  # noqa: PLC0415
                writer = _tool_call_writer.get()
                logger.debug(f"{tool_name}: _tool_call_writer={'SET' if writer else 'NONE'}")  # noqa: G004

                async for event in deep_agent_graph.astream(
                    child_input,
                    config=cast("RunnableConfig", child_config),
                    stream_mode=("messages", "values"),
                    subgraphs=True,
                ):
                    source_label, raw_event = _unpack_subgraph_event(event)
                    if raw_event is None:
                        continue

                    # Values event → state snapshot
                    if isinstance(raw_event, dict):
                        final_state = raw_event
                        continue

                    # Messages event → (chunk, metadata)
                    if not isinstance(raw_event, tuple) or len(raw_event) != 2:
                        continue

                    msg_chunk, metadata = raw_event
                    if not hasattr(msg_chunk, "content"):
                        continue

                    node = (
                        metadata.get("langgraph_node", "subagent")
                        if isinstance(metadata, dict)
                        else "subagent"
                    )
                    logger.debug(
                        f"{tool_name}: chunk src={source_label} node={node} "  # noqa: G004
                        f"type={type(msg_chunk).__name__}",
                    )

                    _emit_message_deltas(runtime, msg_chunk, metadata, tool_name, source_label)

            except GraphInterrupt:
                logger.info(f"{tool_name} paused for human input (interrupt)")  # noqa: G004
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{tool_name} streaming failed", extra={"error": str(exc)})  # noqa: G004
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content=f"{tool_name} encountered an error: {exc}\n\nThe {phase_name} coordinator was unable to complete the request.",
                                tool_call_id=tool_call_id,
                                name=tool_name,
                            )
                        ],
                        "status": "error",
                    }
                )

            # ── Post-stream: transform output ─────────────────────────
            try:
                if final_state is None:
                    logger.error(
                        f"{tool_name} yielded no state — check "
                        f"_unpack_subgraph_event for event format changes",
                    )
                    msg = f"{tool_name} yielded no state"
                    raise AgentExecutionError(msg)  # noqa: TRY301

                if isinstance(final_state, dict):
                    child_state_dict: dict[str, Any] = cast("dict[str, Any]", final_state)
                elif hasattr(final_state, "model_dump"):
                    child_state_dict = cast("dict[str, Any]", final_state.model_dump())
                else:
                    child_state_dict = cast("dict[str, Any]", dict(final_state))

                payload = coordinator.output_transform(child_state_dict)

                wf = k8sAutopilotSupervisorAgent._coerce_workflow_state(dict(runtime.state))
                wf.set_phase_complete(phase_name)
                wf.last_agent = tool_name
                wf.next_agent = None

                final_msg_content = payload.get("final_message") or f"{tool_name} completed."
                coordinator_status = payload.get("status", "completed")

                # Build a tool message that clearly signals completion to
                # the supervisor LLM.  When the coordinator used an interrupt
                # (e.g. request_chat_continue) to deliver data directly to
                # the user, the final_message is often just a farewell.
                # Without an explicit status prefix the supervisor LLM may
                # think the coordinator failed and retry with another agent.
                tool_msg_content = (
                    f"[{tool_name}] Status: {coordinator_status}. "
                    f"The coordinator has finished its task and delivered "
                    f"the results to the user.\n\n"
                    f"Coordinator final message: {final_msg_content}"
                )
                tool_msg = ToolMessage(content=tool_msg_content, tool_call_id=tool_call_id, name=tool_name)

                is_handoff = _detect_cross_domain_handoff(final_msg_content)
                effective_status = "handoff" if is_handoff else "completed"

                update_dict: dict[str, Any] = {
                    "workflow_state": wf,
                    output_key: payload,
                    "messages": [tool_msg],
                    "status": effective_status,
                    "workflow_complete": False if is_handoff else wf.workflow_complete,
                }

                if "domain_summary" in payload:
                    update_dict["domain_summaries"] = [payload["domain_summary"]]

                if is_handoff:
                    handoff_ctx = _extract_handoff_context(
                        message=final_msg_content, source_tool=tool_name, payload=payload,
                    )
                    update_dict["cross_domain_context"] = handoff_ctx
                    logger.info(
                        f"{tool_name} cross-domain handoff detected",  # noqa: G004
                        extra={
                            "source_domain": handoff_ctx.get("source_domain"),
                            "user_request": handoff_ctx.get("user_request", "")[:100],
                        },
                    )

                return Command(update=update_dict)

            except Exception as exc:  # noqa: BLE001
                logger.error(f"{tool_name} post-processing failed", extra={"error": str(exc)})  # noqa: G004
                return Command(
                    update={
                        "messages": [ToolMessage(content=f"{tool_name} completed but result processing failed: {exc}", tool_call_id=tool_call_id)],
                        "status": "error",
                    }
                )

        _coordinator_tool.__doc__ = tool_doc
        return _coordinator_tool

    def _create_agent_tools(self, primary_coordinator: BaseDeepAgent) -> list:
        """Create tool wrappers for each coordinator (data-driven)."""
        tools: list[Any] = []

        for tool_name, agent_key, output_key, phase_name, doc in _COORDINATOR_SPECS:
            # First spec always uses primary_coordinator
            coord = primary_coordinator if not tools else self.agents.get(agent_key)
            if coord is None:
                continue
            tools.append(self._make_coordinator_tool(
                coordinator=coord,
                tool_name=tool_name,
                tool_doc=doc,
                output_key=output_key,
                phase_name=phase_name,
            ))

        return tools

    @staticmethod
    def _make_request_human_feedback_tool():
        """Create the HITL tool for greetings/out-of-scope/clarification."""

        @tool
        def request_human_feedback(
            question: str,
            context: str | None = None,
            tool_call_id: Annotated[str, InjectedToolCallId] = "",
        ) -> Command:
            """Request human feedback during workflow execution.

            Use when:
            - User sends greetings — create friendly, contextual greeting
            - Request is out-of-scope — guide user to Helm/ArgoCD tasks
            - Need clarification on ambiguous requirements

            Args:
                question: Dynamic, contextual message for the human
                context: Optional context about why feedback is needed
            """
            if not tool_call_id:
                tool_call_id = "unknown"

            logger.info(
                "Requesting human feedback",
                extra={"question_preview": question[:200], "tool_call_id": tool_call_id},
            )

            payload = {
                "pending_feedback_requests": {
                    "status": "input_required",
                    "question": question,
                    "context": context or "No additional context provided",
                    "tool_name": "request_human_feedback",
                },
            }

            response = interrupt(payload)
            response_str = str(response) if response is not None else ""
            tool_msg = ToolMessage(content=f"Human input received: {response_str}", tool_call_id=tool_call_id)

            return Command(
                update={
                    "pending_feedback_requests": {},
                    "messages": [tool_msg],
                    "user_query": response_str,
                    "status": "working",
                },
            )

        return request_human_feedback

    # ── Streaming ─────────────────────────────────────────────────────

    @log_async
    async def stream(
        self,
        query: str | Command,
        context_id: str,
        task_id: str,
        use_ui: bool = False,  # noqa: ARG002, FBT001, FBT002
    ) -> AsyncGenerator[AgentResponse, None]:
        """Stream graph execution, yielding AgentResponse objects."""
        if not self._graph:
            msg = "Supervisor graph not constructed"
            raise RuntimeError(msg)

        is_resume = isinstance(query, Command)

        if isinstance(query, Command):
            stream_input = await self._resolve_resume_input(query, context_id)
        else:
            stream_input = self._build_initial_input(str(query), context_id, task_id)

        logger.info(
            "Starting supervisor stream",
            extra={"task_id": task_id, "context_id": context_id, "is_resume": is_resume},
        )

        rec_limit = getattr(self.config_instance, "recursion_limit", 50)
        config = cast(
            "RunnableConfig",
            {"configurable": {"thread_id": context_id, "recursion_limit": rec_limit}},
        )

        async for response in self._run_stream(stream_input, config, context_id, task_id):
            yield response

    async def _resolve_resume_input(self, query: Command, context_id: str) -> Command:
        """Parse a resume Command, expanding HITL decisions if needed."""
        resume_val = query.resume
        if isinstance(resume_val, str):
            try:
                resume_val = json.loads(resume_val)
            except json.JSONDecodeError:
                pass

        # Check for simple approve/reject that needs HITL batch expansion
        decision_type: str | None = None
        if isinstance(resume_val, str) and resume_val in ("approve", "reject"):
            decision_type = resume_val
        elif isinstance(resume_val, dict) and "decision" in resume_val:
            decision_type = resume_val["decision"]

        if not decision_type:
            return Command(resume=resume_val)

        # Check if pending interrupt is from HumanInTheLoopMiddleware
        state_config = cast("RunnableConfig", {"configurable": {"thread_id": context_id}})
        action_count = await self._get_pending_action_count(state_config)

        if action_count > 0:
            logger.info(
                "Expanding single decision for batch HITL resume",
                extra={"decision_type": decision_type, "action_count": action_count},
            )
            return Command(resume={"decisions": [{"type": decision_type} for _ in range(action_count)]})

        # Direct interrupt() — pass through unchanged
        logger.info(
            "Non-HITL-middleware interrupt — passing resume value through unchanged",
            extra={"decision_type": decision_type},
        )
        return Command(resume=resume_val)

    # ── Core v3 streaming loop ────────────────────────────────────────

    async def _run_stream(
        self,
        stream_input: Any,
        config: RunnableConfig,
        context_id: str,
        task_id: str,
    ) -> AsyncGenerator[AgentResponse, None]:
        """Core streaming loop using LangGraph v3 typed projections."""
        assert self._graph is not None  # noqa: S101

        def _make_working(content: Any, **meta_extra: Any) -> AgentResponse:
            """Factory for working-state AgentResponse objects."""
            return AgentResponse(
                content=content,
                response_type="token",
                is_task_complete=False,
                require_user_input=False,
                metadata={"context_id": context_id, "task_id": task_id, "status": "working", **meta_extra},
            )

        try:
            import warnings  # noqa: PLC0415
            warnings.filterwarnings("ignore", message=".*v3 streaming protocol.*", category=Warning)
            run = await self._graph.astream_events(stream_input, config=config, version="v3")
            async with run:
                queue: asyncio.Queue[AgentResponse | None] = asyncio.Queue()

                # ── Message projection consumer ───────────────────
                async def _consume_messages() -> None:
                    """Consume run.messages + run.subgraphs (merged)."""

                    async def _drain_message(message: Any, source: str = "supervisor") -> None:
                        """Process a single message projection entry."""
                        node = message.node or "agent"

                        # Reasoning tokens (provider-agnostic)
                        try:
                            async for delta_text in message.reasoning:
                                if delta_text:
                                    await queue.put(_make_working(
                                        str(delta_text),
                                        node=node, source=source, message_type="reasoning",
                                    ))
                        except (AttributeError, TypeError):
                            pass

                        # Text tokens
                        try:
                            async for delta_text in message.text:
                                if delta_text:
                                    await queue.put(_make_working(str(delta_text), node=node, source=source))
                        except (AttributeError, TypeError):
                            pass

                        # Finalized tool calls (supervisor-level)
                        try:
                            finalized = message.tool_calls.get()
                        except (AttributeError, TypeError):
                            try:
                                finalized = await message.tool_calls
                            except (AttributeError, TypeError):
                                finalized = None
                            except Exception as exc:
                                logger.warning(f"tool_calls error: {type(exc).__name__}: {exc}")  # noqa: G004
                                finalized = None

                        for tc in finalized or []:
                            tc_name, raw_args = _extract_tc_fields(tc)
                            if not tc_name:
                                continue
                            args_display = _format_tool_args(raw_args)
                            content_lines = f"> **Tool Call** · `{tc_name}`  \n"
                            if args_display:
                                content_lines += f"> **Input:** {args_display}  \n"
                            content_lines += "\n"
                            await queue.put(_make_working(
                                content_lines,
                                node=node, message_type="tool_call", tool_name=tc_name, tool_args=raw_args,
                            ))

                    # Drain supervisor-level messages
                    async for message in run.messages:
                        await _drain_message(message, source="supervisor")

                # ── Subgraph projection consumer (merged) ─────────
                async def _consume_subgraphs() -> None:
                    """Consume run.subgraphs — coordinator reasoning + text."""
                    async for subgraph in run.subgraphs:
                        source = subgraph.graph_name or (subgraph.path[-1] if subgraph.path else "subagent")
                        async for msg in subgraph.messages:
                            node = msg.node or "subagent"

                            try:
                                async for delta_text in msg.reasoning:
                                    if delta_text:
                                        await queue.put(_make_working(
                                            str(delta_text),
                                            source=source, node=node, message_type="reasoning",
                                        ))
                            except (AttributeError, TypeError):
                                pass

                            try:
                                async for delta_text in msg.text:
                                    if delta_text:
                                        await queue.put(_make_working(str(delta_text), source=source, node=node))
                            except (AttributeError, TypeError):
                                pass

                # ── Tool-call projection consumer ─────────────────
                async def _consume_tool_calls() -> None:
                    """Consume run.tool_calls — tool lifecycle + output deltas."""
                    if not hasattr(run, "tool_calls"):
                        return

                    async for tool_call in run.tool_calls:
                        name = getattr(tool_call, "tool_name", "tool")

                        # Delegation label for transfer_to_*
                        if name.startswith("transfer_to_"):
                            friendly = _humanize_tool_name(name)
                            await queue.put(_make_working(
                                f"🤖 **Delegated to {friendly}**\n\n",
                                source=name, message_type="delegation",
                            ))

                        # Deep agent output deltas
                        async for delta in tool_call.output_deltas:
                            if not isinstance(delta, dict):
                                continue

                            delta_type = delta.get("type", "")
                            source = delta.get("source", name)
                            handler = _DELTA_HANDLERS.get(delta_type)
                            if handler:
                                await handler(delta, source, name, queue, _make_working)

                        # Final tool result (supervisor-level)
                        output = tool_call.output
                        is_error = tool_call.error is not None
                        tc_display = _humanize_tool_name(name)
                        emoji = "❌" if is_error else "✅"
                        snippet = _sanitize_result_snippet(output)
                        display = f"> {emoji} **{tc_display}** — {snippet or 'completed.'}\n\n"
                        await queue.put(_make_working(
                            display, message_type="tool_result", tool_name=name,
                        ))

                # ── Launch concurrent consumers ────────────────────
                producer_error: BaseException | None = None

                async def _produce() -> None:
                    nonlocal producer_error
                    try:
                        await asyncio.gather(
                            _consume_messages(),
                            _consume_tool_calls(),
                            _consume_subgraphs(),
                        )
                    except BaseException as exc:  # noqa: BLE001
                        producer_error = exc
                    finally:
                        await queue.put(None)

                producer = asyncio.create_task(_produce())

                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item

                await producer
                if producer_error is not None:
                    raise producer_error

            # ── Post-stream: interrupt detection ──────────────────
            if await run.interrupted():
                interrupts = tuple(await run.interrupts())

                tool_name = _extract_interrupt_tool_name(interrupts)
                if tool_name:
                    yield AgentResponse(
                        content=f"> **Result** · `{tool_name}` — completed, awaiting user input.\n\n",
                        response_type="token",
                        is_task_complete=False,
                        require_user_input=False,
                        metadata={
                            "context_id": context_id, "task_id": task_id,
                            "status": "working", "message_type": "tool_result", "tool_name": tool_name,
                        },
                    )

                yield self._build_interrupt_response(interrupts, context_id, task_id)
                return

            # ── Completion ────────────────────────────────────────
            output = await run.output()
            if output:
                yield self._build_v3_completion(output, context_id, task_id)
            else:
                yield AgentResponse(
                    content="Workflow completed.",
                    response_type="text",
                    is_task_complete=True,
                    require_user_input=False,
                    metadata={"context_id": context_id, "task_id": task_id, "status": "completed"},
                )

        except Exception:  # noqa: BLE001
            logger.exception("Stream execution failed", extra={"task_id": task_id, "context_id": context_id})
            yield AgentResponse(
                response_type="error",
                is_task_complete=True,
                require_user_input=False,
                content="Error during streaming",
                error="stream_error",
                metadata={"context_id": context_id, "task_id": task_id, "status": "error"},
            )
        finally:
            try:
                from langchain_core.tracers.langchain import wait_for_all_tracers  # noqa: PLC0415
                wait_for_all_tracers()
            except (ImportError, Exception):  # noqa: BLE001, S110
                pass

    # ── v3 completion handler ──────────────────────────────────────────

    def _build_v3_completion(
        self,
        output: dict[str, Any],
        context_id: str,
        task_id: str,
    ) -> AgentResponse:
        """Build completion response from v3 run.output (final state dict)."""
        messages = output.get("messages", [])
        content = ""
        for msg in reversed(messages):
            msg_content = _extract_content_text(getattr(msg, "content", ""))
            if msg_content:
                if _detect_cross_domain_handoff(msg_content):
                    logger.info("Cross-domain handoff detected in v3 output", extra={"preview": msg_content[:200]})
                    return AgentResponse(
                        content=msg_content,
                        response_type="token",
                        is_task_complete=False,
                        require_user_input=False,
                        metadata={"context_id": context_id, "task_id": task_id, "status": "handoff"},
                    )
                content = msg_content
                break

        return AgentResponse(
            content=content or "Workflow completed.",
            response_type="text",
            is_task_complete=True,
            require_user_input=False,
            metadata={"context_id": context_id, "task_id": task_id, "status": "completed"},
        )

    # ── HITL interrupt handling ────────────────────────────────────────

    @staticmethod
    def _build_interrupt_response(
        interrupt_payload: tuple,
        context_id: str,
        task_id: str,
    ) -> AgentResponse:
        """Convert a LangGraph interrupt payload into an AgentResponse."""
        first = interrupt_payload[0] if interrupt_payload else {}
        value = getattr(first, "value", first)

        if not isinstance(value, dict):
            value = {"type": "generic", "data": str(value)}

        # pending_feedback_requests
        feedback_raw = value.get("pending_feedback_requests", {})
        if feedback_raw and isinstance(feedback_raw, dict):
            return AgentResponse(
                content={
                    "type": "human_feedback_request",
                    "question": feedback_raw.get("question", "Input required"),
                    "context": feedback_raw.get("context", ""),
                    "status": feedback_raw.get("status", "input_required"),
                },
                response_type="human_input",
                is_task_complete=False,
                require_user_input=True,
                metadata={
                    "context_id": context_id, "task_id": task_id,
                    "interrupt_type": "human_feedback", "pending_feedback_requests": feedback_raw,
                },
            )

        # action_requests (HITL middleware)
        action_requests: list[dict] = cast(list[dict], value.get("action_requests", []))
        if action_requests:
            summary = k8sAutopilotSupervisorAgent._format_action_requests_summary(action_requests)
            return AgentResponse(
                content={
                    "type": "hitl_approval",
                    "summary": summary,
                    "action_requests": action_requests,
                    "original_interrupt": value,
                },
                response_type="human_input",
                is_task_complete=False,
                require_user_input=True,
                metadata={
                    "context_id": context_id, "task_id": task_id,
                    "interrupt_type": "hitl_approval", "action_request_count": len(action_requests),
                },
            )

        # Custom interrupt types
        custom_type = value.get("type", "")
        if custom_type and custom_type != "generic":
            return AgentResponse(
                content=value,
                response_type="human_input",
                is_task_complete=False,
                require_user_input=True,
                metadata={"context_id": context_id, "task_id": task_id, "interrupt_type": custom_type},
            )

        # Fallback: generic interrupt
        return AgentResponse(
            content={
                "type": "generic_interrupt",
                "message": (
                    value.get("message") or value.get("summary")
                    or value.get("question") or "Human input required"
                ),
                "data": value,
            },
            response_type="human_input",
            is_task_complete=False,
            require_user_input=True,
            metadata={"context_id": context_id, "task_id": task_id, "interrupt_type": "generic"},
        )

    # ── Batch HITL helpers ─────────────────────────────────────────────

    async def _get_pending_action_count(self, config: RunnableConfig) -> int:
        """Count pending HITL action_requests in the graph checkpoint."""
        try:
            assert self._graph is not None
            state = await self._graph.aget_state(config)
            for task in (state.tasks or ()):
                for intr in (task.interrupts or ()):
                    value = getattr(intr, "value", None)
                    if isinstance(value, dict):
                        action_requests = value.get("action_requests", [])
                        if action_requests:
                            count = len(action_requests)
                            logger.info("Detected pending action_requests", extra={"count": count})
                            return max(count, 1)
        except Exception:  # noqa: BLE001
            logger.warning("Could not inspect graph state for pending action count — defaulting to 0")
        return 0

    # ── Human-readable HITL summaries ─────────────────────────────────

    _TOOL_LABELS: dict[str, tuple[str, str]] = {
        # Helm
        "helm_install_chart": ("🚀", "Install"),
        "helm_upgrade_release": ("⬆️", "Upgrade"),
        "helm_rollback_release": ("⏪", "Rollback"),
        "helm_uninstall_release": ("🗑️", "Uninstall"),
        # ArgoCD — Applications
        "create_application": ("➕", "Create App"),
        "update_application": ("✏️", "Update App"),
        "delete_application": ("🗑️", "Delete App"),
        "sync_application": ("🔄", "Sync App"),
        "rollback_application": ("⏪", "Rollback App"),
        "rollback_to_revision": ("⏪", "Rollback Revision"),
        "hard_refresh": ("♻️", "Hard Refresh"),
        "cancel_deployment": ("🛑", "Cancel Deployment"),
        "prune_resources": ("🧹", "Prune Resources"),
        # ArgoCD — Repos & Projects
        "onboard_repository_https": ("📦", "Onboard Repo (HTTPS)"),
        "onboard_repository_ssh": ("🔑", "Onboard Repo (SSH)"),
        "delete_repository": ("🗑️", "Delete Repository"),
        "create_project": ("📁", "Create Project"),
        "delete_project": ("🗑️", "Delete Project"),
        # Argo Rollouts
        "argo_delete_rollout": ("🗑️", "Delete Rollout"),
        "argo_delete_experiment": ("🗑️", "Delete Experiment"),
        "convert_deployment_to_rollout": ("🔄", "Deploy → Rollout"),
        "convert_rollout_to_deployment": ("⏪", "Rollout → Deploy"),
        "argo_manage_rollout_lifecycle": ("🚀", "Rollout Lifecycle"),
        "argo_manage_legacy_deployment": ("⚠️", "Legacy Deploy"),
        # Traefik
        "traefik_manage_weighted_routing": ("🔀", "Weighted Route"),
        "traefik_manage_simple_route": ("🔗", "Simple Route"),
        "traefik_manage_middleware": ("🛡️", "Middleware"),
        "traefik_nginx_migration": ("🔄", "NGINX Migration"),
        "traefik_manage_tcp_routing": ("🔌", "TCP Route"),
        "traefik_configure_service_affinity": ("📌", "Sticky Sessions"),
        # K8s Cluster Operations
        "resources_delete": ("🗑️", "Delete Resource"),
        "pods_delete": ("🗑️", "Delete Pod"),
        "resources_create_or_update": ("📝", "Create/Update Resource"),
        "resources_scale": ("⚖️", "Scale Resource"),
        "pods_exec": ("🔐", "Pod Exec"),
        "pods_run": ("🚀", "Run Pod"),
        # Observability — Prometheus
        "prom_apply_servicemonitor": ("📡", "Apply ServiceMonitor"),
        "prom_apply_probe": ("🩺", "Apply Probe"),
        "prom_install_exporter": ("📦", "Install Exporter"),
        "prom_uninstall_exporter": ("🗑️", "Uninstall Exporter"),
        "prom_upsert_rule_group": ("📐", "Upsert Rule Group"),
        "prom_delete_rule_group": ("🗑️", "Delete Rule Group"),
        "prom_manage_file_sd": ("📝", "Manage File SD"),
        "prom_configure_remote_write": ("🔄", "Configure Remote Write"),
        # Observability — Alertmanager
        "am_push_test_alert": ("🚨", "Push Test Alert"),
        "am_create_silence": ("🔇", "Create Silence"),
        "am_update_silence": ("⏱️", "Update Silence"),
        "am_expire_silence": ("🔊", "Expire Silence"),
        "am_silence_alert": ("🔇", "Silence Alert"),
        # Observability — OpenTelemetry
        "otel_provision_collector": ("📦", "Provision Collector"),
        "otel_patch_collector": ("🔧", "Patch Collector"),
        "otel_patch_instrumentation": ("🔌", "Patch Instrumentation"),
        "otel_annotate_deployment": ("🚀", "Annotate Deployment"),
        "otel_toggle_sampling_strategy": ("📊", "Toggle Sampling"),
        "otel_enable_spanmetrics_for_service": ("📈", "Enable SpanMetrics"),
        # Observability — Tempo
        "tempo_create_operator_cr": ("➕", "Create Tempo CR"),
        "tempo_patch_operator_cr": ("🔧", "Patch Tempo CR"),
    }

    # Entity extraction lookup: keyword → lambda(args) → entity string
    _ENTITY_EXTRACTORS: dict[str, Any] = {
        "repository": lambda a: a.get("repo_url", a.get("name", "unknown")),
        "project": lambda a: a.get("project_name", a.get("name", "unknown")),
        "traefik": lambda a: a.get("route_name") or a.get("middleware_name") or a.get("service_name") or a.get("name", "unknown"),
        "rollout": lambda a: a.get("name") or a.get("rollout_name") or a.get("deployment_name", "unknown"),
        "deployment": lambda a: a.get("name") or a.get("rollout_name") or a.get("deployment_name", "unknown"),
    }

    # Item label lookup: keyword → display label
    _ITEM_LABELS: dict[str, str] = {
        "repository": "repo",
        "project": "project",
        "application": "app",
        "sync": "app",
        "rollout": "rollout",
        "experiment": "rollout",
        "traefik": "route",
        "helm": "release",
    }

    # K8s cluster ops tools (need Kind/Name formatting)
    _K8S_OPS_TOOLS = {
        "resources_delete", "resources_create_or_update", "resources_scale",
        "pods_delete", "pods_exec", "pods_run",
    }

    @staticmethod
    def _format_action_requests_summary(action_requests: list[dict]) -> str:
        """Build a human-readable Markdown summary for batched HITL actions."""
        labels = k8sAutopilotSupervisorAgent._TOOL_LABELS
        extractors = k8sAutopilotSupervisorAgent._ENTITY_EXTRACTORS
        item_labels = k8sAutopilotSupervisorAgent._ITEM_LABELS
        k8s_ops = k8sAutopilotSupervisorAgent._K8S_OPS_TOOLS

        # Group by tool name
        groups: dict[str, list[dict]] = {}
        for req in action_requests:
            if isinstance(req, dict):
                groups.setdefault(req.get("name", "unknown"), []).append(req.get("args", {}))

        lines: list[str] = []
        for tool_name, arg_list in groups.items():
            emoji, label = labels.get(tool_name, ("⚙️", tool_name.replace("_", " ").title()))
            count = len(arg_list)

            # Determine item label
            item_label = "action"
            for keyword, lbl in item_labels.items():
                if keyword in tool_name:
                    item_label = lbl
                    break
            if tool_name in k8s_ops:
                item_label = "pod" if "pods" in tool_name else "resource"

            plural = "s" if count != 1 else ""
            lines.append(f"{emoji} **{label}** ({count} {item_label}{plural}):")

            for args in arg_list:
                # Entity resolution
                entity = None
                for keyword, extractor in extractors.items():
                    if keyword in tool_name:
                        entity = extractor(args)
                        break

                if entity is None and tool_name in k8s_ops:
                    k8s_kind = args.get("kind", "Pod" if "pods" in tool_name else "")
                    k8s_name = args.get("name", "unknown")
                    entity = f"{k8s_kind}/{k8s_name}" if k8s_kind else k8s_name
                elif entity is None:
                    entity = (
                        args.get("release_name") or args.get("chart_name")
                        or args.get("name") or args.get("app_name", "unknown")
                    )

                ns = args.get("destination_namespace") or args.get("dest_namespace") or args.get("namespace", "default")

                extras: list[str] = []
                for key in ("version", "revision", "target_revision"):
                    if key in args:
                        prefix = "v" if key == "version" else "rev "
                        extras.append(f"{prefix}{args[key]}")
                suffix = f" ({', '.join(extras)})" if extras else ""

                if "repository" in tool_name:
                    lines.append(f"  • **{entity}**{suffix}")
                else:
                    lines.append(f"  • **{entity}** → namespace: `{ns}`{suffix}")
            lines.append("")

        return "\n".join(lines).strip() or "Action requires approval."

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _build_initial_input(query: str, context_id: str, task_id: str) -> dict[str, Any]:
        """Build the initial input dict for a new conversation."""
        return {
            "messages": [HumanMessage(content=query)],
            "user_query": query,
            "session_id": context_id,
            "task_id": task_id,
            "workflow_state": SupervisorWorkflowState(current_phase="requirements"),
            "status": "pending",
        }

    @staticmethod
    def _coerce_workflow_state(state: dict[str, Any]) -> SupervisorWorkflowState:
        """Coerce workflow_state to a SupervisorWorkflowState instance."""
        existing = state.get("workflow_state")
        if isinstance(existing, SupervisorWorkflowState):
            return existing
        if isinstance(existing, dict):
            return SupervisorWorkflowState(**existing)
        return SupervisorWorkflowState()

    @log_sync
    def is_ready(self) -> bool:
        """Check if the supervisor is ready for use."""
        return bool(self.model and self._graph and (self.agents or self._coordinator))

    @log_sync
    def list_agents(self) -> list[str]:
        """List all available agents."""
        return list(self.agents.keys())


# ---------------------------------------------------------------------------
# Delta dispatch handlers
# ---------------------------------------------------------------------------

async def _handle_token_delta(
    delta: dict, source: str, name: str,
    queue: asyncio.Queue, make_response: Any,
) -> None:
    text = delta.get("text", "")
    if not text:
        return
    meta: dict[str, Any] = {"node": delta.get("node", "subagent"), "source": source}
    if delta.get("delta_type") == "reasoning-delta":
        meta["message_type"] = "reasoning"
    await queue.put(make_response(text, **meta))


async def _handle_tool_call_delta(
    delta: dict, source: str, name: str,
    queue: asyncio.Queue, make_response: Any,
) -> None:
    tc_name = delta.get("tool_name", "tool")
    tc_display = _humanize_tool_name(tc_name)
    tc_input = delta.get("tool_input")
    args_display = _format_tool_args(tc_input)
    content = f"> **Tool Call** · `{tc_display}`  \n"
    if args_display:
        content += f"> **Input:** {args_display}  \n"
    content += "\n"
    await queue.put(make_response(
        content,
        node=delta.get("node", "subagent"), source=source,
        message_type="tool_call", tool_name=tc_name, tool_display_name=tc_display,
    ))


async def _handle_tool_started_delta(
    delta: dict, source: str, name: str,
    queue: asyncio.Queue, make_response: Any,
) -> None:
    tc_name = delta.get("tool_name", "tool")
    tc_display = _humanize_tool_name(tc_name)
    tc_input = delta.get("tool_input")
    args_display = _format_tool_args(tc_input)
    content = f"> 🔧 **{tc_display}**  \n"
    if args_display:
        content += f"> Input: {args_display}  \n"
    content += "\n"
    await queue.put(make_response(
        content,
        source=source, message_type="tool_started",
        tool_name=tc_name, tool_display_name=tc_display,
    ))


async def _handle_tool_finished_delta(
    delta: dict, source: str, name: str,
    queue: asyncio.Queue, make_response: Any,
) -> None:
    tc_name = delta.get("tool_name", "tool")
    tc_display = _humanize_tool_name(tc_name)
    tc_error = delta.get("error")
    tc_output = delta.get("output", "")
    emoji = "❌" if tc_error else "✅"
    snippet = _sanitize_result_snippet(tc_output)
    display = f"> {emoji} **{tc_display}** — {snippet or 'completed.'}\n\n"
    await queue.put(make_response(
        display,
        source=source, message_type="tool_result",
        tool_name=tc_name, tool_display_name=tc_display,
    ))


_DELTA_HANDLERS: dict[str, Any] = {
    "token": _handle_token_delta,
    "tool_call": _handle_tool_call_delta,
    "tool_started": _handle_tool_started_delta,
    "tool_finished": _handle_tool_finished_delta,
}


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_k8sAutopilotSupervisorAgent(  # noqa: N802
    agents: list[BaseSubgraphAgent] | None = None,
    config: Config | None = None,
    custom_config: dict[str, Any] | None = None,
    prompt_template: str | None = None,
    name: str = "k8sAutopilotSupervisorAgent",
    *,
    coordinator: BaseDeepAgent | None = None,
    coordinators: list[BaseDeepAgent] | None = None,
) -> k8sAutopilotSupervisorAgent:
    """Create a supervisor agent with centralized configuration."""
    return k8sAutopilotSupervisorAgent(
        agents=agents,
        config=config,
        custom_config=custom_config,
        prompt_template=prompt_template,
        name=name,
        coordinator=coordinator,
        coordinators=coordinators,
    )
