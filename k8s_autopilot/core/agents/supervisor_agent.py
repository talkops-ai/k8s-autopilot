"""K8s Autopilot Supervisor Agent — pure router delegating to coordinators."""

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from typing import Any, cast, Literal
from pydantic import BaseModel, Field
from langchain.tools import tool
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StreamMode, StreamWriter, interrupt

from k8s_autopilot.config.config import Config
from k8s_autopilot.core.state.base import (
    MainSupervisorState,
    SupervisorWorkflowState,
)
from k8s_autopilot.core.state.handoff_contracts import (
    check_loop_guard,
    increment_loop_guard,
    format_handoff_for_context,
)

from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.logger import AgentLogger, log_async, log_sync


from .supervisor_middleware import build_supervisor_middleware  # noqa: F401 — kept for backward compat
from .types import AgentResponse, BaseAgent, BaseDeepAgent, BaseSubgraphAgent

logger = AgentLogger("SupervisorAgent")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

class RouterDecision(BaseModel):
    """Routing decision for the K8s Autopilot Supervisor."""
    destination: Literal[
        "transfer_to_helm_operator",
        "transfer_to_k8s_operator",
        "transfer_to_app_operator",
        "transfer_to_observability_operator",
        "request_human_feedback",
    ] = Field(
        description="The target coordinator for the user's query. Use request_human_feedback for out-of-scope requests."
    )
    task: str = Field(
        description="A concise technical task description for the destination, or the clarifying question to ask the user if destination is request_human_feedback."
    )
    reasoning: str = Field(
        description="Brief explanation of why this route was chosen."
    )

SUPERVISOR_PROMPT = """\
<role>
You are the K8s Autopilot router.
Classify each request and route it to exactly one coordinator tool.
Do not perform any operational work yourself.
</role>

<destinations>
- transfer_to_helm_operator: Helm chart creation, update, validation.
- transfer_to_k8s_operator: Kubernetes cluster operations and diagnostics.
- transfer_to_app_operator: ArgoCD, Argo Rollouts, and Traefik traffic control.
- transfer_to_observability_operator: Prometheus, Alertmanager, OpenTelemetry, Loki, and Tempo.
- request_human_feedback: Out-of-scope, unclear, or non-infrastructure requests.
</destinations>

<decision_rules>
- Route by primary operational intent.
- If a request spans multiple domains, choose the first actionable infrastructure step.
- For read-only checks, route by the resource being inspected.
- For telemetry, logging, tracing, alerting, and instrumentation, route to observability.
- If the request is unclear or unrelated, route to human feedback.
</decision_rules>

<task_rules>
Return a concise technical task for the chosen destination.
Normalize user wording into DevOps terminology.
Do not copy the user message verbatim.
CRITICAL: If the destination is `request_human_feedback`, the `task` field MUST be the exact conversational response or clarifying question you want to display directly to the user (e.g., "Hi! How can I help you with Kubernetes today?"). Do NOT output instructions like "Acknowledge the user".
</task_rules>

<cross_domain>
If a coordinator says it is outside its scope and includes a User Request and Context:
- re-route immediately,
- prefix the new task with:
  [CROSS-DOMAIN] Source: {source_domain}. Prior findings: {context_summary}. User Request: {user_request}
- do not ask the user for permission.
</cross_domain>

<output_contract>
Follow the JSON schema exactly.
Do not output natural language outside of the schema.
</output_contract>
"""


# ---------------------------------------------------------------------------
# Cross-domain handoff logic has moved to core/state/handoff_contracts.py
# ---------------------------------------------------------------------------


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

# Node name mapping: tool names <-> StateGraph node names (architecture spec)
_TOOL_TO_NODE: dict[str, str] = {
    "transfer_to_helm_operator": "helm_agent",
    "transfer_to_k8s_operator": "k8s_ops_agent",
    "transfer_to_app_operator": "app_mgmt_agent",
    "transfer_to_observability_operator": "observability_agent",
}
# Mapping logic remains. Handoff target keywords moved to handoff_contracts.py.


def _extract_tc_fields(tc: Any) -> tuple[str, dict[str, Any] | None]:
    """Extract name and args from a tool call (dict or object)."""
    if isinstance(tc, dict):
        return tc.get("name", ""), tc.get("args")
    return getattr(tc, "name", ""), getattr(tc, "args", None)


# ---------------------------------------------------------------------------
# SupervisorAgent
# ---------------------------------------------------------------------------

class k8sAutopilotSupervisorAgent(BaseAgent):  # noqa: N801
    """Supervisor agent — StateGraph with deterministic stack-based routing.

    Architecture (docs/k8s_autopilot_architecture_spec.md):
      Entry → supervisor_router → classify_request → coordinator node(s) → finalize_response
      Cross-domain handoff uses dialog_state stack (push/pop) for call-and-return.

    The old ``create_agent()`` + tool-wrapper approach has been replaced by a
    ``StateGraph`` where each coordinator is a dedicated node.  Routing is
    deterministic after the initial LLM classification.
    """

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
        """Build supervisor as a StateGraph with deterministic stack-based routing.

        Architecture: docs/k8s_autopilot_architecture_spec.md

        Nodes:
          supervisor_router  — deterministic hub; processes handoff push/pop
          classify_request   — LLM-based initial intent classification
          <coordinator>      — helm_agent | k8s_ops_agent | app_mgmt_agent | observability_agent
          error_handler      — structured error recovery
          finalize_response  — terminal output

        Routing (deterministic after classification):
          1. error_state set        → error_handler
          2. dialog_state non-empty → top of stack
          3. fresh request          → classify_request
          4. otherwise              → finalize_response
        """
        if self._coordinator is None:
            msg = "No coordinator configured for supervisor"
            raise RuntimeError(msg)

        builder = StateGraph(MainSupervisorState)  # type: ignore[bad-specialization]

        # ── Nodes ─────────────────────────────────────────────
        builder.add_node("supervisor_router", self._supervisor_router_node)  # type: ignore[arg-type]
        builder.add_node("classify_request", self._classify_request_node)  # type: ignore[arg-type]
        builder.add_node("error_handler", self._error_handler_node)  # type: ignore[arg-type]
        builder.add_node("finalize_response", self._finalize_response_node)  # type: ignore[arg-type]

        # Coordinator nodes (lazy-init deep agent invocation)
        available_nodes: list[str] = []
        for tool_name, agent_key, output_key, phase_name, _doc in _COORDINATOR_SPECS:
            node_name = _TOOL_TO_NODE[tool_name]
            coord = self._coordinator if not available_nodes else self.agents.get(agent_key)
            if coord is None:
                continue
            builder.add_node(
                node_name,
                self._make_coordinator_node(
                    coordinator=coord,
                    node_name=node_name,
                    tool_name=tool_name,
                    output_key=output_key,
                    phase_name=phase_name,
                ),
            )
            available_nodes.append(node_name)

        self._available_coordinator_nodes: set[str] = set(available_nodes)

        # ── Edges ─────────────────────────────────────────────
        builder.add_edge(START, "supervisor_router")

        # supervisor_router → conditional routing
        route_map: dict[str, str] = {
            "classify_request": "classify_request",
            "error_handler": "error_handler",
            "finalize_response": "finalize_response",
            END: END,
        }
        for nn in available_nodes:
            route_map[nn] = nn

        builder.add_conditional_edges(
            "supervisor_router",
            self._route_after_supervisor,
            route_map,  # type: ignore[arg-type]
        )

        # Hub-and-spoke: all domain nodes → supervisor_router
        builder.add_edge("classify_request", "supervisor_router")
        for nn in available_nodes:
            builder.add_edge(nn, "supervisor_router")
        builder.add_edge("error_handler", "supervisor_router")

        # Terminal
        builder.add_edge("finalize_response", END)

        logger.info(
            "Supervisor StateGraph built (stack-based routing)",
            extra={
                "coordinator_nodes": available_nodes,
                "total_nodes": len(available_nodes) + 4,
            },
        )

        return builder.compile(checkpointer=cast("MemorySaver", self.memory))

    # ── Tool wrappers (Legacy code removed) ───────────────────────────

    # ── Stack-based routing nodes (bidirectional handoff) ─────────────

    async def _supervisor_router_node(
        self, state: dict[str, Any], config: RunnableConfig,
    ) -> dict[str, Any]:
        """Process handoff state transitions (push/pop) before routing.

        This node does NOT decide routing — that is the job of
        ``_route_after_supervisor`` (the conditional edge function).
        It processes incoming ``handoff_request`` / ``handoff_result``
        envelopes to update the ``dialog_state`` stack.
        """
        updates: dict[str, Any] = {}

        # ── Process handoff_request → push target onto stack ──────
        handoff_req = state.get("handoff_request")
        if isinstance(handoff_req, dict) and handoff_req.get("target_agent"):
            target = handoff_req["target_agent"]
            correlation_id = handoff_req.get("correlation_id", "")

            # Loop guard
            if check_loop_guard(state.get("loop_guard"), correlation_id):
                logger.warning(
                    "Loop guard triggered — too many handoffs",
                    extra={"correlation_id": correlation_id, "target": target},
                )
                updates["error_state"] = {
                    "type": "loop_detected",
                    "correlation_id": correlation_id,
                    "message": f"Too many handoffs for correlation {correlation_id}",
                }
                updates["handoff_request"] = {}
                return updates

            updates["dialog_state"] = target  # reducer pushes
            updates["active_agent"] = target
            updates["return_to"] = handoff_req.get("return_to", "")
            updates["resume_cursor"] = handoff_req.get("resume_cursor", "")
            updates["correlation_id"] = correlation_id
            updates["handoff_request"] = {}  # consumed
            updates["loop_guard"] = increment_loop_guard(
                state.get("loop_guard"), correlation_id,
            )

            # ── Update user_query so the target coordinator gets
            # the ACTUAL cross-domain request, not the original query.
            intent = handoff_req.get("intent", "")
            payload_data = handoff_req.get("payload", {})
            user_request = (
                payload_data.get("user_request", "") if isinstance(payload_data, dict) else ""
            ) or intent
            if user_request:
                updates["user_query"] = user_request
                updates["messages"] = [HumanMessage(content=user_request)]

            logger.info(
                "Stack push (handoff request)",
                extra={
                    "target": target,
                    "return_to": updates["return_to"],
                    "stack_depth": len(state.get("dialog_state", [])) + 1,
                    "forwarded_query": user_request[:100] if user_request else "(unchanged)",
                },
            )
            return updates

        # ── Process handoff_result → callee already popped itself ─
        #
        # Architecture spec §Routing model: the callee has already popped
        # itself off dialog_state. Now we pop the CALLER as well, because
        # the cross-domain delegation round-trip is complete. The caller
        # does not need to re-execute — it already did its work before
        # requesting the handoff, and the callee has now fulfilled it.
        #
        # Without this pop, the caller would be re-invoked with the stale
        # handoff intent as user_query, causing an infinite ping-pong.
        handoff_res = state.get("handoff_result")
        if isinstance(handoff_res, dict) and handoff_res.get("correlation_id"):
            return_to = handoff_res.get("target_agent", "")
            callee = handoff_res.get("source_agent", "")
            callee_summary_raw = handoff_res.get("summary", "")
            # Extract clean user-facing text (strips thinking blocks,
            # signatures, and other internal Gemini content-block metadata).
            from k8s_autopilot.core.state.handoff_contracts import (  # noqa: PLC0415
                _extract_clean_text,
            )
            callee_summary = _extract_clean_text(callee_summary_raw)

            summary = format_handoff_for_context(handoff_res)

            # Pop the caller off the stack — the round-trip is complete.
            # NOTE: We compute the popped stack explicitly (as a list) rather
            # than returning the string "pop", because LangGraph may coerce a
            # bare string to list("pop") → ['p','o','p'] before calling the
            # reducer. Returning a list triggers the reducer's overwrite path.
            current_stack = list(state.get("dialog_state", []))
            if current_stack:
                current_stack = current_stack[:-1]
            updates["dialog_state"] = current_stack
            updates["active_agent"] = ""
            updates["status"] = "completed"

            # Update user_query so finalize_response has proper context
            updates["user_query"] = (
                f"[Cross-domain delegation completed]\n"
                f"Caller: {return_to} → Callee: {callee}\n"
                f"Result: {callee_summary[:500]}"
            )
            # summary already includes "[Cross-domain result] ..." header
            updates["messages"] = [
                SystemMessage(content=summary),
            ]
            updates["handoff_result"] = {}  # consumed
            # Clear stale handoff tracking fields
            updates["return_to"] = ""
            updates["resume_cursor"] = ""
            updates["correlation_id"] = ""
            logger.info(
                "Handoff result consumed — caller popped (round-trip complete)",
                extra={
                    "caller": return_to,
                    "callee": callee,
                    "status": handoff_res.get("status"),
                    "stack_depth": len(state.get("dialog_state", [])),
                },
            )
            return updates

        return updates

    def _route_after_supervisor(self, state: dict[str, Any]) -> str:
        """Deterministic routing based on dialog_state stack.

        Routing algorithm (architecture spec §Routing model):
          1. error_state unresolved → error_handler
          2. dialog_state non-empty → top of stack
          3. fresh request (pending) → classify_request
          4. otherwise              → finalize_response
        """
        # 1. Error takes priority
        error = state.get("error_state")
        if isinstance(error, dict) and error.get("type"):
            return "error_handler"

        # 2. Stack-based routing
        stack = state.get("dialog_state", [])
        if stack:
            top = stack[-1]
            if top in self._available_coordinator_nodes:
                return top
            logger.warning(
                f"Unknown agent on stack: {top!r} — routing to error_handler",
                extra={"stack": list(stack)},
            )
            return "error_handler"

        # 3. Fresh request needs classification
        status = state.get("status", "pending")
        if status == "pending":
            return "classify_request"

        # 4. Workflow finished
        return "finalize_response"

    async def _classify_request_node(
        self, state: dict[str, Any], config: RunnableConfig,
    ) -> dict[str, Any]:
        """LLM-based intent classification for initial request routing.

        Uses ``model.bind_tools()`` with lightweight routing tool stubs.
        The LLM picks which coordinator handles the request; the node
        extracts the target from the tool call and pushes it onto
        ``dialog_state``.
        """
        # Bind to structured output
        model_with_tools = self.model.with_structured_output(RouterDecision, include_raw=True)

        # Inject cross-domain context (replaces SupervisorContextMiddleware)
        context_msgs: list[Any] = []
        domain_summaries = state.get("domain_summaries", [])
        if domain_summaries:
            lines = []
            for s in domain_summaries:
                if isinstance(s, dict):
                    domain = s.get("domain", "unknown")
                    outcome = s.get("outcome", "completed")
                    detail = s.get("detail", "")
                    lines.append(f"- **{domain}**: {outcome}" + (f" — {detail}" if detail else ""))
            if lines:
                context_msgs.append(SystemMessage(
                    content=(
                        "## Cross-Domain Context\n"
                        "Previous coordinator outcomes this session:\n"
                        + "\n".join(lines)
                        + "\n\nUse this when routing follow-up requests."
                    ),
                ))

        all_msgs = state.get("messages", [])
        user_query = state.get("user_query", "")
        
        # Fallback if user_query is not set in state
        if not user_query:
            for msg in reversed(all_msgs):
                if isinstance(msg, HumanMessage):
                    user_query = _extract_content_text(getattr(msg, "content", ""))
                    break

        # Build limited chat history for context without overwhelming the router
        chat_history = []
        for m in all_msgs[-5:]:
            role = "User" if isinstance(m, HumanMessage) else "Assistant" if isinstance(m, AIMessage) else "System"
            content = _extract_content_text(getattr(m, "content", ""))
            if content:
                if len(content) > 300:
                    content = content[:300] + " ... [truncated]"
                chat_history.append(f"{role}: {content}")

        history_text = "\n".join(chat_history) if chat_history else "No prior context."

        classification_prompt = (
            f"Recent conversation context (for reference only, these tasks may already be completed):\n"
            f"{history_text}\n\n"
            f"--------------------------------------------------\n"
            f"CURRENT USER REQUEST TO CLASSIFY:\n"
            f"<user_request>\n{user_query}\n</user_request>\n\n"
            f"INSTRUCTION: Focus strictly on the <user_request> above. Route this exact request. Do not route based on past completed tasks."
        )

        messages = [
            SystemMessage(content=self.prompt_template),
            *context_msgs,
            HumanMessage(content=classification_prompt),
        ]

        try:
            response = await model_with_tools.ainvoke(messages, config=config)
            response_dict = cast(dict[str, Any], response)
            decision: RouterDecision | None = response_dict.get("parsed")
            raw_msg = response_dict.get("raw")
        except Exception as exc:
            logger.error("Classification LLM call failed", extra={"error": str(exc)})
            return {
                "error_state": {
                    "type": "classification_error",
                    "message": str(exc),
                },
            }

        # Fallback if parsing failed completely
        if not decision:
            logger.warning("Classification produced no parsed decision")
            content = _extract_content_text(getattr(raw_msg, "content", "")) if raw_msg else "Unknown parsing error."
            payload = {
                "pending_feedback_requests": {
                    "status": "input_required",
                    "question": "I'm not sure how to help. Could you describe your Kubernetes task?",
                    "context": content or "No matching coordinator",
                    "tool_name": "request_human_feedback",
                },
            }
            resume_val = interrupt(payload)
            resume_str = str(resume_val) if resume_val is not None else ""
            msgs = [raw_msg] if raw_msg else []
            msgs.append(HumanMessage(content=resume_str))
            return {
                "pending_feedback_requests": {},
                "messages": msgs,
                "user_query": resume_str,
                "status": "pending",
            }

        # Handle the structured decision
        if decision.destination == "request_human_feedback":
            payload = {
                "pending_feedback_requests": {
                    "status": "input_required",
                    "question": decision.task,
                    "context": decision.reasoning or "No additional context provided",
                    "tool_name": "request_human_feedback",
                },
            }
            resume_val = interrupt(payload)
            resume_str = str(resume_val) if resume_val is not None else ""
            
            return {
                "pending_feedback_requests": {},
                "messages": [raw_msg, HumanMessage(content=resume_str)],
                "user_query": resume_str,
                "status": "pending",  # re-classify after feedback
            }

        target_node = _TOOL_TO_NODE.get(decision.destination)
        if target_node:
            logger.info(
                "Request classified",
                extra={"target": target_node, "reasoning": decision.reasoning, "task_preview": decision.task[:200]},
            )
            return {
                "dialog_state": target_node,   # push onto stack
                "active_agent": target_node,
                "user_query": decision.task,
                "status": "working",
                "messages": [raw_msg],
            }

        # Fallback: unknown destination string
        logger.warning(
            "Classification produced unknown destination", extra={"destination": decision.destination},
        )
        payload = {
            "pending_feedback_requests": {
                "status": "input_required",
                "question": "I'm not sure how to help. Could you describe your Kubernetes task?",
                "context": "No matching coordinator",
                "tool_name": "request_human_feedback",
            },
        }
        resume_val = interrupt(payload)
        resume_str = str(resume_val) if resume_val is not None else ""
        return {
            "pending_feedback_requests": {},
            "messages": [raw_msg, HumanMessage(content=resume_str)],
            "user_query": resume_str,
            "status": "pending",
        }

    def _make_coordinator_node(
        self,
        coordinator: BaseDeepAgent,
        node_name: str,
        tool_name: str,
        output_key: str,
        phase_name: str,
    ) -> Any:
        """Create a StateGraph node that invokes a BaseDeepAgent coordinator.

        Each coordinator node:
          1. Lazy-inits the deep agent graph
          2. Transforms supervisor state → coordinator input
          3. Streams the deep agent (astream with config passthrough)
          4. Transforms output → supervisor state update
          5. Detects cross-domain handoff → sets handoff_request
          6. Or completes → pops dialog_state
        """

        async def _coordinator_node(
            state: dict[str, Any], config: RunnableConfig, *, writer: StreamWriter,
        ) -> dict[str, Any]:
            logger.info(
                f"{node_name} coordinator node invoked",
                extra={
                    "session_id": state.get("session_id"),
                    "task_id": state.get("task_id"),
                    "user_query_preview": state.get("user_query", "")[:200],
                },
            )

            # ── Lazy-init the deep agent graph ────────────────────
            if not coordinator._is_initialized:
                logger.info(f"Building {node_name} deep agent graph")
                coordinator._deep_agent_graph = await coordinator.build_agent()
                coordinator._is_initialized = True

            deep_graph = coordinator._deep_agent_graph
            if deep_graph is None:
                return {
                    "error_state": {
                        "type": "init_failed",
                        "agent": node_name,
                        "message": f"{node_name} deep agent graph not initialized",
                    },
                    "dialog_state": "pop",
                }

            # ── Build input ───────────────────────────────────────
            task = state.get("user_query", "")



            send_payload: dict[str, Any] = dict(state)
            send_payload["messages"] = [HumanMessage(content=task)]
            send_payload["user_query"] = task
            child_input = coordinator.input_transform(send_payload)

            # ── Build config ──────────────────────────────────────
            child_config: dict[str, Any] = {
                k: v for k, v in config.items() if k not in ("store", "callbacks")
            }
            configurable = dict(config.get("configurable", {}))

            # Store bridging
            child_store = getattr(deep_graph, "store", None)
            if child_store is None:
                bound = getattr(deep_graph, "bound", None)
                if bound is not None:
                    child_store = getattr(bound, "store", None)

            runtime_obj = configurable.get("__pregel_runtime")
            if (
                runtime_obj is not None
                and hasattr(runtime_obj, "override")
                and child_store is not None
            ):
                configurable["__pregel_runtime"] = runtime_obj.override(
                    store=child_store,
                )

            child_config["configurable"] = {
                **configurable,
                "thread_id": f"{state.get('session_id', 'default')}:{tool_name}",
                "context": coordinator.build_context(
                    supervisor_state=dict(state),
                ),
            }
            child_config["recursion_limit"] = 250

            # ── Invoke deep agent ─────────────────────────────────
            try:
                final_state = None
                async for chunk in deep_graph.astream(
                    child_input,
                    config=cast("RunnableConfig", child_config),
                    stream_mode=cast("list[StreamMode]", ["messages", "values"]),
                    subgraphs=True,
                    version="v2",
                ):
                    chunk_type = chunk.get("type", "") if isinstance(chunk, dict) else ""

                    if chunk_type == "messages":
                        # Forward LLM tokens to parent via custom stream
                        msg_data = chunk.get("data")
                        if msg_data is not None:
                            writer({
                                "kind": "deep_agent_message",
                                "node": node_name,
                                "data": msg_data,
                                "ns": chunk.get("ns", ()),
                            })

                    elif chunk_type == "values":
                        # Capture final state from values stream
                        val = chunk.get("data")
                        if isinstance(val, dict):
                            final_state = val

                    # Other chunk types — ignore silently

            except GraphInterrupt as gi:
                # Check if the interrupt is a chat_continue carrying a
                # handoff/scope-refusal.  If so, DON'T re-raise — instead
                # intercept and return a handoff_request so the supervisor
                # routes to the correct coordinator.
                try:
                    from k8s_autopilot.core.state.handoff_contracts import (  # noqa: PLC0415
                        extract_handoff_from_text,
                    )
                    interrupts = gi.args[0] if gi.args else []
                    for intr in (interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]):
                        intr_val = getattr(intr, "value", intr)
                        if not isinstance(intr_val, dict):
                            continue
                        # Ensure no handoff extraction happens for free-form text on interrupt
                except Exception:  # noqa: BLE001
                    pass  # fallback: re-raise the interrupt normally
                logger.info(f"{node_name} paused for human input (interrupt)")
                raise
            except Exception as exc:
                import traceback
                logger.error(
                    f"{node_name} execution failed\n{traceback.format_exc()}",
                    extra={"error": str(exc)},
                )
                return {
                    "status": "error",
                    "error_state": {
                        "type": "execution_error",
                        "agent": node_name,
                        "message": str(exc),
                    },
                    "dialog_state": "pop",
                }

            # ── Transform output ──────────────────────────────────
            try:
                if final_state is None:
                    logger.error(f"{node_name} yielded no state")
                    return {
                        "status": "error",
                        "error_state": {
                            "type": "no_state", "agent": node_name,
                            "message": f"{node_name} yielded no state",
                        },
                        "dialog_state": "pop",
                    }

                child_dict: dict[str, Any]
                if isinstance(final_state, dict):
                    child_dict = cast("dict[str, Any]", final_state)
                elif hasattr(final_state, "model_dump"):
                    child_dict = cast("dict[str, Any]", final_state.model_dump())
                else:
                    child_dict = cast("dict[str, Any]", dict(final_state))

                payload_out = coordinator.output_transform(child_dict)
                final_msg = payload_out.get("final_message", f"{node_name} completed.")

                # ── Escalation from deep agent tool? ──────────────
                # The escalate_to_supervisor tool sets a structured
                # marker in the deep agent's state.  Detect it here
                # and translate into a re-classification cycle:
                #   pop current agent → status=pending → classify_request
                escalation = child_dict.get("escalation_request")
                if not escalation:
                    # Fallback: Extract from the messages array because deepagents
                    # drops unmapped state fields like escalation_request
                    messages = child_dict.get("messages", [])
                    for i in range(len(messages) - 1, -1, -1):
                        msg = messages[i]
                        if getattr(msg, "type", None) == "ai" and hasattr(msg, "tool_calls"):
                            for tc in msg.tool_calls:
                                if tc.get("name") == "escalate_to_supervisor":
                                    args = tc.get("args", {})
                                    escalation = {
                                        "user_request": args.get("user_request", ""),
                                        "reason": args.get("reason", "")
                                    }
                                    break
                            if escalation:
                                break
                if escalation and isinstance(escalation, dict):
                    esc_user_req = str(escalation.get("user_request") or task or "")
                    esc_reason = str(escalation.get("reason") or "Out of scope")

                    logger.info(
                        f"{node_name} escalation via tool — re-routing",
                        extra={
                            "user_request": esc_user_req[:200],
                            "reason": esc_reason[:200],
                        },
                    )

                    return {
                        output_key: {
                            "final_message": esc_reason,
                            "status": "escalated",
                        },
                        "user_query": esc_user_req,
                        "dialog_state": "pop",
                        "active_agent": "",
                        "status": "pending",
                        "messages": [HumanMessage(content=esc_user_req)],
                    }

                # ── Cross-domain handoff? ─────────────────────────
                if "handoff_request" in payload_out:
                    hr = payload_out["handoff_request"]
                    logger.info(
                        f"{node_name} cross-domain handoff detected natively",
                        extra={
                            "target": hr.get("target_agent"),
                            "intent": hr.get("intent", "")[:100],
                        },
                    )
                    return {
                        output_key: payload_out,
                        "handoff_request": hr,
                        # Don't pop — stay on stack for call-and-return
                        "status": "handoff",
                    }

                # ── Cross-domain RETURN (callee completing delegated task) ─
                callee_return_to = state.get("return_to", "")
                callee_corr_id = state.get("correlation_id", "")
                if callee_return_to and callee_corr_id:
                    # This coordinator was invoked as a callee for another
                    # coordinator.  Produce a HandoffResult so the caller
                    # gets structured cross-domain results.
                    from k8s_autopilot.core.state.handoff_contracts import (  # noqa: PLC0415
                        HandoffResult,
                    )

                    result_payload = {
                        k: v for k, v in payload_out.items()
                        if k not in ("final_message", "handoff_request")
                    }
                    handoff_result = HandoffResult(
                        source_agent=tool_name,
                        target_agent=callee_return_to,
                        correlation_id=callee_corr_id,
                        status=payload_out.get("status", "completed"),
                        summary=final_msg[:500],
                        payload=result_payload,
                    )
                    logger.info(
                        f"{node_name} cross-domain return → {callee_return_to}",
                        extra={
                            "correlation_id": callee_corr_id,
                            "summary_preview": final_msg[:100],
                        },
                    )
                    return {
                        output_key: payload_out,
                        "handoff_result": handoff_result,
                        "dialog_state": "pop",  # pop callee off stack
                        "active_agent": callee_return_to,
                        "status": "handoff_return",
                        # Clear consumed handoff tracking fields
                        "return_to": "",
                        "resume_cursor": "",
                        "correlation_id": "",
                    }

                # ── Normal completion ─────────────────────────────
                wf = k8sAutopilotSupervisorAgent._coerce_workflow_state(state)
                wf.set_phase_complete(phase_name)
                wf.last_agent = tool_name
                wf.next_agent = None

                update: dict[str, Any] = {
                    output_key: payload_out,
                    "dialog_state": "pop",
                    "active_agent": "",
                    "status": "completed",
                    "workflow_state": wf,
                    "workflow_complete": wf.workflow_complete,
                    "messages": [HumanMessage(content=final_msg)],
                }
                if "domain_summary" in payload_out:
                    update["domain_summaries"] = [payload_out["domain_summary"]]
                return update

            except Exception as exc:
                import traceback as tb
                logger.error(
                    f"{node_name} post-processing failed",
                    extra={"error": str(exc), "traceback": tb.format_exc()},
                )
                return {
                    "status": "error",
                    "error_state": {
                        "type": "post_process_error",
                        "agent": node_name,
                        "message": str(exc),
                    },
                    "dialog_state": "pop",
                }

        _coordinator_node.__name__ = node_name
        _coordinator_node.__qualname__ = f"k8sAutopilotSupervisorAgent.{node_name}"
        return _coordinator_node

    async def _error_handler_node(
        self, state: dict[str, Any], config: RunnableConfig,
    ) -> dict[str, Any]:
        """Handle errors: loop detection, execution failures, etc."""
        error = state.get("error_state", {})
        error_type = error.get("type", "unknown")
        error_msg = error.get("message", "An error occurred")

        logger.error(
            "Error handler invoked",
            extra={"error_type": error_type, "error_msg": error_msg},
        )

        # Loop detection → interrupt for human review
        if error_type == "loop_detected":
            payload = {
                "pending_feedback_requests": {
                    "status": "input_required",
                    "question": (
                        f"A routing loop was detected: {error_msg}. "
                        "Would you like to retry with a different approach, "
                        "or provide more specific instructions?"
                    ),
                    "context": f"Error type: {error_type}",
                    "tool_name": "error_handler",
                },
            }
            resume_val = interrupt(payload)
            resume_str = str(resume_val) if resume_val is not None else ""
            clear_stack = []
            for _ in state.get("dialog_state", []):
                clear_stack.append("pop")
            return {
                "error_state": {},
                "pending_feedback_requests": {},
                "dialog_state": clear_stack[0] if clear_stack else [],
                "messages": [HumanMessage(content=resume_str)],
                "user_query": resume_str,
                "status": "pending",
            }

        # General error → inform user
        payload = {
            "pending_feedback_requests": {
                "status": "input_required",
                "question": (
                    f"An error occurred during processing: {error_msg}. "
                    "Would you like to retry or try a different request?"
                ),
                "context": f"Error type: {error_type}",
                "tool_name": "error_handler",
            },
        }
        resume_val = interrupt(payload)
        resume_str = str(resume_val) if resume_val is not None else ""
        return {
            "error_state": {},
            "pending_feedback_requests": {},
            "messages": [HumanMessage(content=resume_str)],
            "user_query": resume_str,
            "status": "pending",
        }

    async def _finalize_response_node(
        self, state: dict[str, Any], config: RunnableConfig,
    ) -> dict[str, Any]:
        """Terminal node — marks workflow complete."""
        return {
            "status": "completed",
            "workflow_complete": True,
            "active_agent": "",
        }

    # ── Legacy tools removed ─────────────────────────────────────────

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
        decision_message: str | None = None
        
        if isinstance(resume_val, str):
            if resume_val.startswith("reject:"):
                decision_type = "reject"
                decision_message = resume_val[7:].strip()
            elif resume_val in ("approve", "reject"):
                decision_type = resume_val
        elif isinstance(resume_val, dict):
            if "decision" in resume_val:
                decision_type = resume_val["decision"]
                decision_message = resume_val.get("message")
            elif "decisions" in resume_val:
                # If it's already properly formatted for HITL, pass it through
                return Command(resume=resume_val)

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
            decision_obj = {"type": decision_type}
            if decision_message:
                decision_obj["message"] = decision_message
                
            return Command(resume={"decisions": [decision_obj for _ in range(action_count)]})

        # Direct interrupt() — pass through unchanged
        if decision_message:
            return Command(resume=f"{decision_type}: {decision_message}")
        return Command(resume=decision_type)

    # ── Core v2 streaming loop ────────────────────────────────────────

    async def _run_stream(
        self,
        stream_input: Any,
        config: RunnableConfig,
        context_id: str,
        task_id: str,
    ) -> AsyncGenerator[AgentResponse, None]:
        """Core streaming loop using LangGraph v2 StreamPart format.

        Uses ``astream(stream_mode=["messages", "updates"], subgraphs=True,
        version="v2")`` instead of ``astream_events(version="v3")``.

        **Why**: ``astream_events(v3)`` injects a ``StreamEventsHandler`` into
        the async callback context via ``contextvars``.  This handler
        propagates into coordinator nodes → deep agent subgraphs → the
        Gemini LLM provider, where it triggers a legacy V2 streaming
        path in ``langchain_google_genai`` that corrupts the reasoning
        payload ("Thought signature is not valid" 400 error).

        ``astream(version="v2")`` uses internal channel-based streaming
        and does NOT inject callback handlers, so the Gemini reasoning
        engine operates normally.

        Each chunk is a ``StreamPart`` dict with ``type``, ``ns``, ``data``:
        - ``type="messages"`` → ``data`` is ``(message_chunk, metadata)``
        - ``type="updates"``  → ``data`` is ``{node_name: state_update}``
        """
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

        # Track current agent for delegation labels
        _current_agent: str = ""
        _seen_delegations: set[str] = set()
        _interrupt_payload: list[Any] = []
        _final_output: dict[str, Any] = {}

        try:
            async for chunk in self._graph.astream(
                stream_input,
                config=config,
                stream_mode=cast("list[StreamMode]", ["updates", "custom"]),
                subgraphs=True,
                version="v2",
            ):
                chunk_type = chunk.get("type", "")
                chunk_ns = chunk.get("ns", ())
                chunk_data = chunk.get("data")

                # ── Identify source from namespace ────────────────
                # ns=() → supervisor level
                # ns=("helm_agent:xxx",) → coordinator subgraph
                # ns=("helm_agent:xxx", "tools:yyy") → deep agent tool
                is_subgraph = bool(chunk_ns)
                source = "supervisor"
                if chunk_ns:
                    # Use the first namespace segment as source
                    first_ns = chunk_ns[0] if chunk_ns else ""
                    # Extract the node name from "node_name:run_id" format
                    source = first_ns.split(":")[0] if ":" in first_ns else first_ns

                # NOTE: 'messages' stream mode removed from parent — the supervisor
                # is a deterministic router with no LLM. All deep-agent LLM tokens
                # are relayed via StreamWriter → 'custom' channel below.

                # ── Custom stream: forwarded deep agent tokens ─────
                if chunk_type == "custom" and isinstance(chunk_data, dict):
                    kind = chunk_data.get("kind", "")
                    if kind == "deep_agent_message":
                        fwd_data = chunk_data.get("data")
                        fwd_node = chunk_data.get("node", "agent")
                        fwd_ns = chunk_data.get("ns", ())
                        if fwd_data is not None:
                            try:
                                fwd_msg, fwd_meta = fwd_data
                            except (TypeError, ValueError):
                                pass
                            else:
                                fwd_source = fwd_node
                                if fwd_ns:
                                    first_seg = fwd_ns[0] if fwd_ns else ""
                                    fwd_source = first_seg.split(":")[0] if ":" in first_seg else first_seg

                                fwd_agent = fwd_meta.get("lc_agent_name", "") if isinstance(fwd_meta, dict) else ""
                                fwd_display = fwd_agent or fwd_source or fwd_node

                                # Delegation label
                                if fwd_display != _current_agent and fwd_display != "supervisor":
                                    _current_agent = fwd_display
                                    if fwd_display not in _seen_delegations:
                                        _seen_delegations.add(fwd_display)
                                        friendly = _humanize_tool_name(fwd_display)
                                        yield _make_working(
                                            f"🤖 **Delegated to {friendly}**\n\n",
                                            source=fwd_display,
                                            message_type="delegation",
                                        )

                                if isinstance(fwd_msg, (AIMessage, AIMessageChunk)):
                                    # Reasoning tokens (provider-agnostic)
                                    reasoning = _extract_reasoning_text(fwd_msg)
                                    if reasoning:
                                        yield _make_working(
                                            str(reasoning),
                                            node=fwd_node,
                                            source=fwd_display,
                                            message_type="reasoning",
                                        )
                                    # Text content
                                    text = _extract_content_text(getattr(fwd_msg, "content", ""))
                                    if text:
                                        yield _make_working(
                                            text,
                                            node=fwd_node,
                                            source=fwd_display,
                                        )
                                    # Tool call chunks
                                    tc_chunks_fwd: list[dict[str, Any]] = getattr(fwd_msg, "tool_call_chunks", []) or []
                                    for tc in tc_chunks_fwd:
                                        tc_n = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                                        if tc_n:
                                            yield _make_working(
                                                f"> 🔧 **{_humanize_tool_name(tc_n)}**  \n\n",
                                                node=fwd_node,
                                                source=fwd_display,
                                                message_type="tool_call",
                                                tool_name=tc_n,
                                            )
                                elif isinstance(fwd_msg, ToolMessage):
                                    t_name = getattr(fwd_msg, "name", "tool")
                                    snippet = _sanitize_result_snippet(getattr(fwd_msg, "content", ""))
                                    status = getattr(fwd_msg, "status", "success")
                                    emoji = "❌" if status == "error" else "✅"
                                    yield _make_working(
                                        f"> {emoji} **{_humanize_tool_name(t_name)}** — {snippet or 'completed.'}\n\n",
                                        source=fwd_display,
                                        message_type="tool_result",
                                        tool_name=t_name,
                                    )

                # ── Updates stream: node completions + interrupts ──
                elif chunk_type == "updates" and isinstance(chunk_data, dict):
                    # Check for interrupts
                    interrupt_data = chunk_data.get("__interrupt__")
                    if interrupt_data:
                        if isinstance(interrupt_data, (list, tuple)):
                            _interrupt_payload.extend(interrupt_data)
                        else:
                            _interrupt_payload.append(interrupt_data)
                        continue

                    # Track final output from finalize_response or last node
                    for node_name, update in chunk_data.items():
                        if isinstance(update, dict):
                            _final_output.update(update)

            # ── Post-stream: interrupt detection ──────────────────
            if _interrupt_payload:
                interrupts = tuple(_interrupt_payload)
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
            if _final_output:
                yield self._build_v3_completion(_final_output, context_id, task_id)
            else:
                yield AgentResponse(
                    content="Workflow completed.",
                    response_type="text",
                    is_task_complete=True,
                    require_user_input=False,
                    metadata={"context_id": context_id, "task_id": task_id, "status": "completed"},
                )

        except Exception:  # noqa: BLE001
            import traceback; traceback.print_exc()
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
        """Build completion response from v3 run.output (final state dict).

        NOTE: ``output`` is ``_final_output`` — a dict built by merging raw
        node return values via ``dict.update()``.  It does NOT pass through
        LangGraph's reducer, so ``dialog_state`` may contain the raw string
        ``"pop"`` instead of the actual reduced stack.  We normalize here.
        """
        # Normalize dialog_state from raw node output
        raw_stack = output.get("dialog_state", [])
        if isinstance(raw_stack, str):
            # "pop" → stack was being emptied; any other string is a push
            # that should have been followed by more nodes (shouldn't appear
            # here as the last update in practice).
            stack: list[str] = [] if raw_stack == "pop" else [raw_stack]
        else:
            stack = list(raw_stack) if raw_stack else []

        # Also check status — "completed" is a reliable completion signal
        # that overrides a stale/malformed stack
        status = output.get("status", "")
        is_complete = (not stack) or status == "completed"

        if not is_complete:
            logger.info(
                "v3 completion — dialog_state non-empty, treating as in-progress",
                extra={"stack": stack, "status": status},
            )
            return AgentResponse(
                content="Processing continues...",
                response_type="token",
                is_task_complete=False,
                require_user_input=False,
                metadata={"context_id": context_id, "task_id": task_id, "status": "working"},
            )

        messages = output.get("messages", [])
        content = ""
        for msg in reversed(messages):
            msg_content = _extract_content_text(getattr(msg, "content", ""))
            if msg_content:
                content = msg_content
                break

        # Strip internal cross-domain routing metadata — show only
        # the summary text to the end user.
        if content.startswith("[Cross-domain result]"):
            # Extract the text after "Summary: " prefix
            summary_idx = content.find("Summary: ")
            if summary_idx != -1:
                content = content[summary_idx + len("Summary: "):]

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
            "dialog_state": [],
            "active_agent": "",
            "handoff_request": {},
            "handoff_result": {},
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
