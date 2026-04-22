"""
K8s Autopilot Supervisor Agent.

Pure router using the CI-supervisor "tool-wrapper" pattern to delegate ALL
Kubernetes infrastructure requests to domain-specific coordinators.

Architecture::

    A2AExecutor.execute()
      → SupervisorAgent.stream(query, context_id, task_id)
          → compiled_graph.astream(
                input, config,
                stream_mode=["updates","messages"], subgraphs=True, version="v2",
            )
              → yields AgentResponse (working / input_required / completed)

Tool-wrapper delegation (4 tools)::

    SupervisorAgent (create_agent)
      → transfer_to_helm_operator  @tool → HelmOperatorCoordinator → Command
      → transfer_to_k8s_operator   @tool → HelmMgmtCoordinator    → Command
      → transfer_to_app_operator   @tool → ArgoCDCoordinator       → Command
      → request_human_feedback     @tool → interrupt()

Reference: aws-orchestrator-agent SupervisorAgent
"""

import json
import re
from collections.abc import AsyncGenerator
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

from .types import AgentResponse, BaseAgent, BaseDeepAgent, BaseSubgraphAgent

logger = AgentLogger("SupervisorAgent")

# Matches UUID-like strings (namespace segments)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")


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
| Argo Rollouts canary / blue-green / analysis | ``transfer_to_app_operator`` |
| Traefik routing, middleware, traffic mgmt | ``transfer_to_app_operator`` |
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
- "Configure Traefik weighted routing" → transfer_to_app_operator

**OUT-OF-SCOPE REQUEST HANDLING:**
If a request is NOT related to Helm/ArgoCD/K8s/Traefik:
1. **CRITICAL: Use request_human_feedback** - no direct text
2. **Create dynamic, contextual messages** for the user
3. **NEVER output text without calling request_human_feedback**

Available tools:
- transfer_to_helm_operator: Helm chart generation/update
- transfer_to_k8s_operator: K8s cluster ops (pods, scale, exec)
- transfer_to_app_operator: ArgoCD, Argo Rollouts, Traefik
- request_human_feedback: Human feedback or clarification

**TASK DESCRIPTION CRAFTING (for transfer_to_* tools):**
Craft a human-readable task_description from the user's intent.
Parse the request, extract intent, create a clear description.
Do NOT pass raw user messages verbatim.

**CRITICAL RULES:**
- Always call tools immediately, don't describe what you will do
- Do NOT do chart generation/validation/K8s ops yourself
- You are a ROUTER, not a CREATOR
""".strip()


# ---------------------------------------------------------------------------
# Module-level helpers (v2 streaming)
# ---------------------------------------------------------------------------

def _iter_messages(node_data: dict[str, Any]):
    """Yield individual messages from a node's update data."""
    messages = node_data.get("messages", [])
    if hasattr(messages, "value"):
        messages = messages.value
    if not messages:
        return
    if not isinstance(messages, list):
        messages = [messages]
    yield from messages


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


def _extract_text_from_tool_chunks(chunks: Any, fields: tuple) -> str:
    """Extract AI-reasoning text from Gemini-style tool_call_chunks."""
    if not chunks:
        return ""
    for tcc in chunks:
        args_str = (
            tcc.get("args", "")
            if isinstance(tcc, dict)
            else getattr(tcc, "args", "")
        )
        if not args_str or not isinstance(args_str, str):
            continue
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            continue
        for key in fields:
            val = args.get(key, "")
            if val and str(val).strip():
                return str(val).strip()
    return ""


def _source_label(ns: tuple) -> str:
    """Derive a human-readable label from the v2 namespace tuple."""
    if not ns:
        return "coordinator"
    for seg in reversed(ns):
        if not isinstance(seg, str):
            continue
        raw_name = seg
        if raw_name.startswith("tools:"):
            raw_name = raw_name.split(":", 1)[1]
        if _UUID_RE.match(raw_name):
            continue
        for prefix in ("transfer_to_", "call_", "invoke_"):
            if raw_name.startswith(prefix):
                raw_name = raw_name[len(prefix):]
                break
        label = raw_name.replace("_", " ").strip()
        if label:
            return label
    return "subagent"


# ---------------------------------------------------------------------------
# SupervisorAgent
# ---------------------------------------------------------------------------

class k8sAutopilotSupervisorAgent(BaseAgent):  # noqa: N801
    """Supervisor agent that manages the K8s Autopilot workflow.

    Uses create_agent() with tool wrappers for each coordinator.
    Streams using v2 mode for dynamic token/progress/interrupt processing.

    Reference: aws-orchestrator-agent SupervisorAgent
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

        # Initialize checkpointer
        try:
            from k8s_autopilot.core.hitl import (  # noqa: PLC0415
                get_checkpointer,
            )
            self.memory = get_checkpointer(
                config=self.config_instance,
                prefer_postgres=True,
            )
        except Exception:  # noqa: BLE001
            self.memory = MemorySaver()

        # Initialize LLM
        self.model = create_model(self.config_instance.get_llm_config())

        # ── Coordinator(s) — new multi-coordinator path ───────────────
        self.agents: dict[str, Any] = {}
        self._coordinator: BaseDeepAgent | None = None

        if coordinators:
            # Multi-coordinator injection (preferred)
            for coord in coordinators:
                self.agents[coord.name] = coord
            # Set primary coordinator (first in list, typically helm-operator)
            self._coordinator = coordinators[0]
        elif coordinator is not None:
            # Single coordinator (backward compat)
            self._coordinator = coordinator
            self.agents[coordinator.name] = coordinator
        else:
            # Legacy path: accept list of BaseSubgraphAgent
            for agent in (agents or []):
                if hasattr(agent, "memory"):
                    agent.memory = self.memory
                self.agents[agent.name] = agent

        self.prompt_template = prompt_template or SUPERVISOR_PROMPT

        # Build supervisor graph
        self._graph = self._build_supervisor_graph()

        mode = (
            "coordinators" if coordinators
            else ("coordinator" if self._coordinator else "legacy")
        )
        logger.info(
            "Supervisor agent initialized",
            extra={
                "mode": mode,
                "agent_count": len(self.agents),
                "agent_names": list(self.agents.keys()),
            },
        )

    # ── BaseAgent interface ───────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    # ── Graph builder ─────────────────────────────────────────────────

    def _build_supervisor_graph(self) -> CompiledStateGraph:
        """Build supervisor using create_agent() with tool wrappers.

        Coordinator mode: transfer_to_helm/k8s/app_operator tools.
        """
        if self._coordinator is None:
            msg = "No coordinator configured for supervisor"
            raise RuntimeError(msg)

        agent_tools = self._create_agent_tools(self._coordinator)

        feedback_tool = self._make_request_human_feedback_tool()
        all_tools = [*agent_tools, feedback_tool]

        mode = "coordinator" if self._coordinator else "legacy"
        logger.info(
            "Creating supervisor with create_agent()",
            extra={
                "tool_names": [t.name for t in all_tools],
                "mode": mode,
            },
        )

        return create_agent(
            model=self.model,
            tools=all_tools,
            system_prompt=self.prompt_template,
            state_schema=MainSupervisorState,
            checkpointer=cast("MemorySaver", self.memory),
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
        """Create a coordinator tool wrapper.

        Reusable factory that builds a @tool wrapper for any BaseDeepAgent
        coordinator, following the AWS orchestrator reference pattern:
        - Inject HumanMessage(task_description) into messages
        - Lazy-init deep agent graph
        - Bridge __pregel_runtime store for MemoryMiddleware
        - Call 3-arg output_transform(agent_state, supervisor_state, tool_call_id)

        Args:
            coordinator: The deep agent coordinator to wrap.
            tool_name: Name of the tool (e.g. 'transfer_to_helm_operator').
            tool_doc: Docstring for the tool.
            output_key: Key in supervisor state for coordinator output.

        Reference: aws-orchestrator SupervisorAgent._create_agent_tools
        """

        @tool(tool_name)  # type: ignore[call-overload]
        async def _coordinator_tool(
            task_description: str,
            runtime: ToolRuntime[None, MainSupervisorState],
            tool_call_id: Annotated[str, InjectedToolCallId],
            config: RunnableConfig,
        ) -> Command:
            """
            Delegate to the deep agent coordinator.

            task_description: intent-based summary from user request.
            Do NOT pass the raw user message verbatim.
            """
            logger.info(
                f"{tool_name} invoked",
                extra={
                    "task_description": task_description[:200],
                    "session_id": runtime.state.get("session_id"),
                    "task_id": runtime.state.get("task_id"),
                },
            )

            # Lazy-init the deep agent graph (JIT pattern)
            if not coordinator._is_initialized:
                logger.info(f"Building {tool_name} deep agent graph lazily")  # noqa: G004
                coordinator._deep_agent_graph = (
                    await coordinator.build_agent()
                )
                coordinator._is_initialized = True

            deep_agent_graph = coordinator._deep_agent_graph
            if deep_agent_graph is None:
                msg = f"{tool_name}: deep agent graph not initialized"
                raise AgentExecutionError(msg)

            # Build input: inject HumanMessage(task_description) into messages
            # (AWS reference pattern — supervisor crafts the message)
            send_payload: dict[str, Any] = dict(runtime.state)
            send_payload["messages"] = [HumanMessage(content=task_description)]
            send_payload["user_query"] = task_description
            child_input = coordinator.input_transform(send_payload)

            # Build config: coordinator owns what context the deep agent needs
            child_config: dict[str, Any] = {
                k: v for k, v in config.items() if k != "store"
            }

            # ── Store bridging (AWS reference pattern) ─────────────────────
            # The outer config carries __pregel_runtime with the supervisor's
            # store=None. We MUST override it with the deep agent's InMemoryStore
            # so MemoryMiddleware works inside the deep agent graph.
            child_store = getattr(deep_agent_graph, "store", None)
            if child_store is None:
                bound = getattr(deep_agent_graph, "bound", None)
                if bound is not None:
                    child_store = getattr(bound, "store", None)

            configurable = dict(config.get("configurable", {}))

            runtime_obj = configurable.get("__pregel_runtime")
            if (
                runtime_obj is not None
                and hasattr(runtime_obj, "override")
                and child_store is not None
            ):
                configurable["__pregel_runtime"] = (
                    runtime_obj.override(store=child_store)
                )

            child_config["configurable"] = {
                **configurable,
                "thread_id": runtime.state.get("session_id", "default"),
                "context": coordinator.build_context(
                    supervisor_state=dict(runtime.state),
                ),
            }

            child_config["recursion_limit"] = 250

            # Invoke the deep agent
            # Invoke the deep agent
            # CRITICAL: GraphInterrupt/NodeInterrupt MUST propagate naturally
            # so the supervisor graph detects __interrupt__ in its stream.
            # Reference: aws-orchestrator transfer_to_terraform (no try/except)
            try:
                final_state = await deep_agent_graph.ainvoke(
                    child_input,
                    config=cast("RunnableConfig", child_config),
                )
            except GraphInterrupt:
                # Let interrupt exceptions bubble up to the supervisor graph.
                # The supervisor's _run_stream will detect __interrupt__
                # and yield _build_interrupt_response with require_user_input=True.
                logger.info(
                    f"{tool_name} paused for human input (interrupt)",  # noqa: G004
                )
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"{tool_name} execution failed",  # noqa: G004
                    extra={"error": str(exc)},
                )
                msg = f"{tool_name} failed: {exc}"
                raise AgentExecutionError(msg) from exc

            if final_state is None:
                msg = f"{tool_name} yielded no state"
                raise AgentExecutionError(msg)

            # Coerce to dict
            if isinstance(final_state, dict):
                child_state_dict: dict[str, Any] = cast("dict[str, Any]", final_state)
            elif hasattr(final_state, "model_dump"):
                child_state_dict = cast("dict[str, Any]", final_state.model_dump())
            else:
                child_state_dict = cast("dict[str, Any]", dict(final_state))

            # Fetch structured output payload from coordinator
            payload = coordinator.output_transform(child_state_dict)

            # Update workflow state
            wf = k8sAutopilotSupervisorAgent._coerce_workflow_state(dict(runtime.state))
            wf.set_phase_complete(phase_name)
            wf.last_agent = tool_name
            wf.next_agent = None

            # Build tool message for the coordinator
            final_msg_content = payload.get("final_message") or f"{tool_name} completed."
            tool_msg = ToolMessage(
                content=final_msg_content,
                tool_call_id=tool_call_id,
            )

            return Command(
                update={
                    "workflow_state": wf.model_dump(),
                    output_key: payload.get(output_key, {}),
                    "messages": [tool_msg],
                    "status": "completed",
                    "workflow_complete": wf.workflow_complete,
                }
            )

        _coordinator_tool.__doc__ = tool_doc
        return _coordinator_tool

    def _create_agent_tools(self, primary_coordinator: BaseDeepAgent) -> list:
        """Create tool wrappers for each coordinator.

        3 tools — one per domain coordinator:
        - transfer_to_helm_operator: Helm chart generation/update
        - transfer_to_k8s_operator: K8s cluster operations
        - transfer_to_app_operator: App onboarding (ArgoCD, Argo Rollouts, Traefik)

        Reference: aws-orchestrator SupervisorAgent._create_agent_tools
        """
        tools: list[Any] = []

        # ── 1. Helm Operator (chart generation & updates) ─────────────────
        tools.append(self._make_coordinator_tool(
            coordinator=primary_coordinator,
            tool_name="transfer_to_helm_operator",
            tool_doc=(
                "Delegate Helm chart generation/update to Helm Operator.\n\n"
                "Use for:\n"
                "- Creating new Helm charts from scratch\n"
                "- Updating existing chart templates\n"
                "- Committing chart files to GitHub\n"
                "- Any chart creation or modification operation"
            ),
            output_key="helm_operator_output",
            phase_name="helm_operator",
        ))

        # ── 2. K8s Operator (cluster operations) ──────────────────────────
        k8s_op = self.agents.get("k8s-operator-coordinator")
        if k8s_op:
            tools.append(self._make_coordinator_tool(
                coordinator=k8s_op,
                tool_name="transfer_to_k8s_operator",
                tool_doc=(
                    "Delegate K8s cluster operations.\n\n"
                    "Use for:\n"
                    "- List/get/create/update/delete K8s resources\n"
                    "- Pod debugging: logs, exec, top, debug pods\n"
                    "- Scaling deployments and replica sets\n"
                    "- Events, node diagnostics, health checks\n"
                    "- Kubeconfig contexts (multi-cluster)"
                ),
                output_key="k8s_operator_output",
                phase_name="k8s_operator",
            ))

        # ── 3. App Operator (ArgoCD + Argo Rollouts + Traefik) ────────────
        app_op = self.agents.get("app-operator-coordinator")
        if app_op:
            tools.append(self._make_coordinator_tool(
                coordinator=app_op,
                tool_name="transfer_to_app_operator",
                tool_doc=(
                    "Delegate application lifecycle operations.\n\n"
                    "Use for:\n"
                    "- ArgoCD: projects, repositories, applications, sync, debug\n"
                    "- Argo Rollouts: canary, blue-green, analysis runs\n"
                    "- Traefik: weighted routing, middleware, traffic mirroring"
                ),
                output_key="app_operator_output",
                phase_name="app_operator",
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
                extra={
                    "question_preview": question[:200],
                    "tool_call_id": tool_call_id,
                },
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

            tool_msg = ToolMessage(
                content=f"Human input received: {response_str}",
                tool_call_id=tool_call_id,
            )

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
        """Stream graph execution, yielding AgentResponse objects.

        Uses v2 streaming with ["updates", "messages"].
        Handles new queries (str) and resume-after-interrupt (Command).
        """
        if not self._graph:
            msg = "Supervisor graph not constructed"
            raise RuntimeError(msg)

        is_resume = isinstance(query, Command)

        # Build input
        if isinstance(query, Command):
            # Intercept raw Command payload passed from A2AExecutor
            resume_val = query.resume
            if isinstance(resume_val, str):
                try:
                    resume_val = json.loads(resume_val)
                except json.JSONDecodeError:
                    pass

            # Determine if this is a simple approve/reject that needs
            # expansion for batch HumanInTheLoopMiddleware interrupts.
            # The middleware expects one decision per action_request.
            decision_type: str | None = None
            if isinstance(resume_val, str) and resume_val in (
                "approve", "reject",
            ):
                decision_type = resume_val
            elif isinstance(resume_val, dict) and "decision" in resume_val:
                decision_type = resume_val["decision"]

            if decision_type:
                # Query the graph's pending interrupt to count
                # how many action_requests need individual decisions.
                state_config = cast(
                    "RunnableConfig",
                    {"configurable": {"thread_id": context_id}},
                )
                action_count = await self._get_pending_action_count(
                    state_config,
                )
                logger.info(
                    "Expanding single decision for batch HITL resume",
                    extra={
                        "decision_type": decision_type,
                        "action_count": action_count,
                    },
                )
                mapped_decision = {
                    "decisions": [
                        {"type": decision_type}
                        for _ in range(action_count)
                    ]
                }
                stream_input = Command(resume=mapped_decision)
            else:
                stream_input = query
        else:
            # All valid resumes are wrapped as Command by A2AExecutor's `_wrap_resume` hook.
            # If we reach here, either the task is not paused, or it's a new conversational prompt.
            stream_input = self._build_initial_input(str(query), context_id, task_id)

        logger.info(
            "Starting supervisor stream",
            extra={
                "task_id": task_id,
                "context_id": context_id,
                "is_resume": is_resume,
            },
        )

        rec_limit = getattr(
            self.config_instance, "recursion_limit", 50,
        )
        config = cast(
            "RunnableConfig",
            {
                "configurable": {
                    "thread_id": context_id,
                    "recursion_limit": rec_limit,
                },
            },
        )

        async for response in self._run_stream(
            stream_input, config, context_id, task_id,
        ):
            yield response

    # ── Core v2 streaming loop ────────────────────────────────────────

    async def _run_stream(
        self,
        stream_input: Any,
        config: RunnableConfig,
        context_id: str,
        task_id: str,
    ) -> AsyncGenerator[AgentResponse, None]:
        """Core streaming loop — processes v2 chunks from the supervisor graph."""
        assert self._graph is not None

        pending_interrupt: tuple | None = None
        completed = False

        try:
            async for chunk in self._graph.astream(
                stream_input,
                config=config,
                stream_mode=["updates", "messages"],
                subgraphs=True,
                version="v2",
            ):
                if not isinstance(chunk, dict):
                    continue

                chunk_type = chunk.get("type")
                ns: tuple = chunk.get("ns", ())
                data = chunk.get("data")
                if data is None:
                    continue

                # ── updates: detect interrupts, completion, progress ──
                if chunk_type == "updates" and isinstance(data, dict):
                    update_data = cast("dict[str, Any]", data)

                    interrupt_payload = self._extract_interrupt(update_data)
                    if interrupt_payload:
                        pending_interrupt = interrupt_payload
                        continue

                    completion = self._extract_completion(
                        ns, update_data, task_id, context_id,
                    )
                    if completion:
                        yield completion
                        completed = True
                        continue

                    for progress in self._extract_progress(
                        ns, update_data, context_id, task_id,
                    ):
                        yield progress

                # ── messages: stream LLM tokens ──
                elif chunk_type == "messages":
                    token_response = self._extract_token(
                        ns, data, context_id, task_id,
                    )
                    if token_response:
                        yield token_response

            # After stream exhausts — handle pending interrupt
            if pending_interrupt:
                yield self._build_interrupt_response(
                    pending_interrupt, context_id, task_id,
                )
                return

            # Generic completion if no explicit one was yielded
            if not completed:
                yield AgentResponse(
                    content="Workflow completed.",
                    response_type="text",
                    is_task_complete=True,
                    require_user_input=False,
                    metadata={
                        "context_id": context_id,
                        "task_id": task_id,
                        "status": "completed",
                    },
                )

        except Exception:  # noqa: BLE001
            logger.exception(
                "Stream execution failed",
                extra={
                    "task_id": task_id,
                    "context_id": context_id,
                },
            )
            yield AgentResponse(
                response_type="error",
                is_task_complete=True,
                require_user_input=False,
                content="Error during streaming",
                error="stream_error",
                metadata={
                    "context_id": context_id,
                    "task_id": task_id,
                    "status": "error",
                },
            )
        finally:
            try:
                from langchain_core.tracers.langchain import (  # noqa: PLC0415
                    wait_for_all_tracers,
                )
                wait_for_all_tracers()
            except (ImportError, Exception):  # noqa: BLE001, S110
                pass

    # ── v2 Chunk processors ───────────────────────────────────────────

    @staticmethod
    def _extract_interrupt(data: dict[str, Any]) -> tuple | None:
        """Detect __interrupt__ in an updates chunk."""
        if "__interrupt__" in data:
            return data["__interrupt__"]
        for v in data.values():
            if isinstance(v, dict) and "__interrupt__" in v:
                return v["__interrupt__"]
        return None

    # Tools that handle HITL flows — their ToolMessages are NOT subagent completions.
    _HITL_TOOL_NAMES: frozenset = frozenset({"request_human_feedback", "request_human_input"})

    def _extract_completion(
        self,
        ns: tuple,
        data: dict[str, Any],
        task_id: str,
        context_id: str,
    ) -> AgentResponse | None:
        """Detect subagent completion in the tools node.

        Guards:
        - Only top-level updates (ns must be empty).
        - Skip in_progress status (e.g. after HITL resume).
        - Skip HITL tool ToolMessages.
        """
        if ns:
            return None

        tools_data = data.get("tools")
        if not tools_data or not isinstance(tools_data, dict):
            return None

        if tools_data.get("status") == "in_progress":
            return None

        messages = tools_data.get("messages", [])
        for msg in messages:
            if getattr(msg, "type", None) != "tool":
                continue

            name = getattr(msg, "name", "subagent")
            if name in self._HITL_TOOL_NAMES:
                continue

            content = _extract_content_text(getattr(msg, "content", ""))
            logger.info(
                "Subagent completed",
                extra={"subagent": name, "preview": content[:200]},
            )
            return AgentResponse(
                content=content,
                response_type="text",
                is_task_complete=True,
                require_user_input=False,
                metadata={
                    "context_id": context_id,
                    "task_id": task_id,
                    "status": "completed",
                    "subagent": name,
                },
            )
        return None

    # ── Formatting constants ──────────────────────────────────────────
    _TOOL_CALL_FMT_SHORT = "> ⚙️ **Tool Call** (`{name}`)  "
    _TOOL_RESULT_FMT = "> {icon} **Result** (`{name}`): {snippet}...\n\n"
    _TOOL_RESULT_FMT_DONE = "> {icon} **Result** (`{name}`) completed.\n\n"
    _AI_TEXT_FIELDS = ("question", "task_description", "description", "message", "query", "content")

    @staticmethod
    def _extract_progress(
        ns: tuple,
        data: dict[str, Any],
        context_id: str,
        task_id: str,
    ) -> list[AgentResponse]:
        """Extract intermediate progress updates from updates chunks."""
        source = _source_label(ns)
        responses: list[AgentResponse] = []

        for node_name, node_data in data.items():
            if not isinstance(node_data, dict):
                continue
            for msg in _iter_messages(node_data):
                msg_type = getattr(msg, "type", None)
                resp = None

                if msg_type == "ai":
                    resp = k8sAutopilotSupervisorAgent._progress_from_ai(
                        msg, source, node_name, context_id, task_id,
                    )
                elif msg_type == "tool" and ns:
                    resp = k8sAutopilotSupervisorAgent._progress_from_tool(
                        msg, source, node_name, context_id, task_id,
                    )

                if resp is not None:
                    responses.append(resp)

        return responses

    @staticmethod
    def _progress_from_ai(
        msg: Any, source: str, node: str, context_id: str, task_id: str,
    ) -> AgentResponse | None:
        """Build a tool-call progress response from an AIMessage."""
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return None

        lines: list[str] = []
        for tc in tool_calls:
            name = (
                tc.get("name") if isinstance(tc, dict)
                else getattr(tc, "name", "tool")
            )
            fmt = k8sAutopilotSupervisorAgent._TOOL_CALL_FMT_SHORT
            lines.append(fmt.format(name=name))

        return AgentResponse(
            content="\n".join(lines) + "\n\n",
            response_type="token",
            is_task_complete=False,
            require_user_input=False,
            metadata={
                "context_id": context_id, "task_id": task_id,
                "status": "working", "message_type": "tool_call",
                "source": source, "node": node,
            },
        )

    @staticmethod
    def _progress_from_tool(
        msg: Any, source: str, node: str, context_id: str, task_id: str,
    ) -> AgentResponse:
        """Build a tool-result progress response from a ToolMessage."""
        tool_name = getattr(msg, "name", "")
        snippet = (
            _extract_content_text(getattr(msg, "content", ""))
            .strip()
            .replace("\n", " ")[:200]
        )

        is_error = getattr(msg, "status", "success") == "error" or any(
            err in snippet.lower()
            for err in ("error", "exception", "failed", "could not")
        )
        icon = "❌" if is_error else "✅"

        cls = k8sAutopilotSupervisorAgent
        if snippet:
            display = cls._TOOL_RESULT_FMT.format(
                icon=icon, name=tool_name, snippet=snippet,
            )
        else:
            display = cls._TOOL_RESULT_FMT_DONE.format(
                icon=icon, name=tool_name,
            )

        return AgentResponse(
            content=display,
            response_type="token",
            is_task_complete=False,
            require_user_input=False,
            metadata={
                "context_id": context_id, "task_id": task_id,
                "status": "working", "message_type": "tool_result",
                "tool_name": tool_name, "source": source, "node": node,
            },
        )

    @staticmethod
    def _extract_token(
        ns: tuple,
        data: Any,
        context_id: str,
        task_id: str,
    ) -> AgentResponse | None:
        """Extract streaming AI text tokens from a messages chunk.

        Two paths:
        1. Standard (OpenAI/Anthropic) — token.content has text.
        2. Gemini — content is [], reasoning is in tool_call_chunks args.
        """
        if not isinstance(data, (list, tuple)) or len(data) < 1:
            return None

        token = data[0]
        chunk_meta = data[1] if len(data) > 1 and isinstance(data[1], dict) else {}
        source = _source_label(ns)
        agent_name = (
            chunk_meta.get("lc_agent_name")
            or chunk_meta.get("langgraph_node")
            or ""
        )

        if getattr(token, "type", None) not in ("ai", "AIMessageChunk"):
            return None

        if getattr(token, "chunk_position", None) == "last":
            return None

        # Path 1: text in content
        text = _extract_content_text(getattr(token, "content", ""))
        if text:
            return k8sAutopilotSupervisorAgent._token_response(
                text, context_id, task_id, source, agent_name,
            )

        # Path 2: Gemini — text in tool_call_chunks
        text = _extract_text_from_tool_chunks(
            getattr(token, "tool_call_chunks", None),
            k8sAutopilotSupervisorAgent._AI_TEXT_FIELDS,
        )
        if text:
            return k8sAutopilotSupervisorAgent._token_response(
                text + "\n\n", context_id, task_id, source, agent_name,
                message_type="tool_call",
            )

        return None

    @staticmethod
    def _token_response(
        text: str, context_id: str, task_id: str, source: str, agent_name: str,
        message_type: str | None = None,
    ) -> AgentResponse:
        """Build a streaming AI-text token response."""
        meta: dict[str, Any] = {
            "context_id": context_id, "task_id": task_id,
            "status": "working", "stream_mode": "messages",
            "source": source, "agent_name": agent_name,
        }
        if message_type:
            meta["message_type"] = message_type
        return AgentResponse(
            content=text,
            response_type="token",
            is_task_complete=False,
            require_user_input=False,
            metadata=meta,
        )

    # ── HITL interrupt handling ────────────────────────────────────────

    @staticmethod
    def _build_interrupt_response(
        interrupt_payload: tuple,
        context_id: str,
        task_id: str,
    ) -> AgentResponse:
        """Convert a LangGraph interrupt payload into an AgentResponse.

        Handles three interrupt shapes:
        1. pending_feedback_requests — request_human_feedback tool pattern
        2. Custom interrupt types — tools that call interrupt() directly
        3. action_requests — HumanInTheLoopMiddleware pattern
        4. phase+summary — HITL gate interrupts
        """
        first = interrupt_payload[0] if interrupt_payload else {}
        value = getattr(first, "value", first)

        if not isinstance(value, dict):
            value = {"type": "generic", "data": str(value)}

        # Branch 1: pending_feedback_requests
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
                    "context_id": context_id,
                    "task_id": task_id,
                    "interrupt_type": "human_feedback",
                    "pending_feedback_requests": feedback_raw,
                },
            )

        # Branch 2: action_requests (HITL middleware — approve/edit/reject)
        action_requests: list[dict] = cast(
            list[dict], value.get("action_requests", []),
        )
        if action_requests:
            summary = k8sAutopilotSupervisorAgent._format_action_requests_summary(
                action_requests,
            )
            action_count = len(action_requests)

            logger.info(
                "HITL interrupt detected",
                extra={
                    "task_id": task_id,
                    "action_count": action_count,
                    "summary_preview": summary[:200],
                },
            )

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
                    "context_id": context_id,
                    "task_id": task_id,
                    "interrupt_type": "hitl_approval",
                    "action_request_count": action_count,
                },
            )

        # Branch 5: Custom interrupt types
        custom_type = value.get("type", "")
        if custom_type and custom_type not in ("generic",):
            return AgentResponse(
                content=value,
                response_type="human_input",
                is_task_complete=False,
                require_user_input=True,
                metadata={
                    "context_id": context_id,
                    "task_id": task_id,
                    "interrupt_type": custom_type,
                },
            )

        # Branch 6: action_requests (HITL middleware — fallback/duplicate)
        action_requests = cast(
            list[dict], value.get("action_requests", []),
        )
        if action_requests:
            summary = (
                k8sAutopilotSupervisorAgent._format_action_requests_summary(
                    action_requests,
                )
            )
            return AgentResponse(
                content={
                    "type": "hitl_approval",
                    "summary": summary,
                    "question": "Do you approve this action?",
                    "action_requests": action_requests,
                },
                response_type="human_input",
                is_task_complete=False,
                require_user_input=True,
                metadata={
                    "context_id": context_id,
                    "task_id": task_id,
                    "interrupt_type": "hitl_approval",
                    "action_request_count": len(action_requests),
                },
            )

        # Fallback: generic interrupt
        return AgentResponse(
            content={
                "type": "generic_interrupt",
                "message": (
                    value.get("message")
                    or value.get("summary")
                    or (value.get("question") if isinstance(value, dict) else None)
                    or "Human input required"
                ),
                "data": value,
            },
            response_type="human_input",
            is_task_complete=False,
            require_user_input=True,
            metadata={
                "context_id": context_id,
                "task_id": task_id,
                "interrupt_type": "generic",
            },
        )

    # ── Batch HITL helpers ─────────────────────────────────────────────

    async def _get_pending_action_count(
        self,
        config: RunnableConfig,
    ) -> int:
        """Inspect graph checkpoint to count pending HITL action_requests.

        When ``HumanInTheLoopMiddleware`` batches multiple tool calls into
        a single interrupt, the resume must include one decision per
        action_request.  This method inspects the saved interrupt to
        determine the correct count.

        Returns:
            Number of pending action_requests (minimum 1).
        """
        try:
            assert self._graph is not None
            state = await self._graph.aget_state(config)
            for task in (state.tasks or ()):
                for intr in (task.interrupts or ()):
                    value = getattr(intr, "value", None)
                    if isinstance(value, dict):
                        action_requests = value.get(
                            "action_requests", [],
                        )
                        if action_requests:
                            count = len(action_requests)
                            logger.info(
                                "Detected pending action_requests "
                                "from graph checkpoint",
                                extra={"count": count},
                            )
                            return max(count, 1)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not inspect graph state for "
                "pending action count — defaulting to 1",
            )
        return 1

    # ── Human-readable HITL summaries ─────────────────────────────────

    # Mapping of tool names to (emoji, human label)
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
    }

    @staticmethod
    def _format_action_requests_summary(
        action_requests: list[dict],
    ) -> str:
        """Build a human-readable Markdown summary for batched HITL actions.

        Groups actions by tool name and formats each with release name
        and namespace instead of raw JSON dumps.

        Example output::

            🗑️ **Uninstall** (3 releases):
              • **argo-cd** → namespace: ``argocd``
              • **ingress-nginx** → namespace: ``mgmt``
              • **traefik** → namespace: ``traefik``
        """
        labels = k8sAutopilotSupervisorAgent._TOOL_LABELS

        # Group by tool name, preserving insertion order
        groups: dict[str, list[dict]] = {}
        for req in action_requests:
            if not isinstance(req, dict):
                continue
            name = req.get("name", "unknown")
            args = req.get("args", {})
            groups.setdefault(name, []).append(args)

        lines: list[str] = []
        for tool_name, arg_list in groups.items():
            emoji, label = labels.get(
                tool_name,
                ("⚙️", tool_name.replace("_", " ").title()),
            )
            count = len(arg_list)

            # Determine dynamic resource label per domain
            if "repository" in tool_name:
                item_label = "repo"
            elif "project" in tool_name:
                item_label = "project"
            elif (
                "application" in tool_name
                or "sync" in tool_name
            ):
                item_label = "app"
            elif (
                "rollout" in tool_name
                or "experiment" in tool_name
            ):
                item_label = "rollout"
            elif "traefik" in tool_name:
                item_label = "route"
            elif "helm" in tool_name:
                item_label = "release"
            elif tool_name in (
                "resources_delete", "resources_create_or_update",
                "resources_scale",
            ):
                item_label = "resource"
            elif tool_name in ("pods_delete", "pods_exec", "pods_run"):
                item_label = "pod"
            else:
                item_label = "action"

            plural = "s" if count != 1 else ""
            lines.append(
                f"{emoji} **{label}** ({count} {item_label}{plural}):",
            )
            for args in arg_list:
                # Multi-domain entity resolution
                if "repository" in tool_name:
                    entity = args.get(
                        "repo_url", args.get("name", "unknown"),
                    )
                elif "project" in tool_name:
                    entity = args.get(
                        "project_name", args.get("name", "unknown"),
                    )
                elif "traefik" in tool_name:
                    entity = (
                        args.get("route_name")
                        or args.get("middleware_name")
                        or args.get("service_name")
                        or args.get("name", "unknown")
                    )
                elif (
                    "rollout" in tool_name
                    or "deployment" in tool_name
                ):
                    entity = (
                        args.get("name")
                        or args.get("rollout_name")
                        or args.get("deployment_name", "unknown")
                    )
                elif tool_name in (
                    "resources_delete", "resources_create_or_update",
                    "resources_scale", "pods_delete", "pods_exec",
                    "pods_run",
                ):
                    # K8s cluster ops: Kind/Name format
                    k8s_kind = args.get("kind", "Pod" if "pods" in tool_name else "")
                    k8s_name = args.get("name", "unknown")
                    entity = f"{k8s_kind}/{k8s_name}" if k8s_kind else k8s_name
                else:
                    entity = (
                        args.get("release_name")
                        or args.get("chart_name")
                        or args.get("name")
                        or args.get("app_name", "unknown")
                    )

                # Multi-domain namespace resolution
                ns = (
                    args.get("destination_namespace")
                    or args.get("dest_namespace")
                    or args.get("namespace", "default")
                )

                extras: list[str] = []
                if "version" in args:
                    extras.append(f"v{args['version']}")
                if "revision" in args:
                    extras.append(f"rev {args['revision']}")
                if "target_revision" in args:
                    extras.append(f"rev {args['target_revision']}")
                suffix = f" ({', '.join(extras)})" if extras else ""

                # Repos show URL only (no namespace)
                if "repository" in tool_name:
                    lines.append(f"  • **{entity}**{suffix}")
                else:
                    lines.append(
                        f"  • **{entity}** → namespace: `{ns}`{suffix}",
                    )
            lines.append("")  # blank line between groups

        return "\n".join(lines).strip() or "Action requires approval."

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _build_initial_input(
        query: str,
        context_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Build the initial input dict for a new conversation."""
        return {
            "messages": [HumanMessage(content=query)],
            "user_query": query,
            "session_id": context_id,
            "task_id": task_id,
            "workflow_state": SupervisorWorkflowState(
                current_phase="requirements",
            ),
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
    """Create a supervisor agent with centralized configuration.

    Args:
        agents: Legacy list of BaseSubgraphAgent (deprecated).
        coordinator: Single primary coordinator (backward compat).
        coordinators: All domain coordinators (preferred).
        config: Configuration object.
        custom_config: Custom configuration dict.
        prompt_template: Optional custom system prompt.
        name: Agent name.

    Usage (multi-coordinator, preferred)::

        from ...helm_operator.coordinator import (
            create_helm_coordinator,
        )
        from ...k8s_operator.coordinator import (
            create_k8s_operator_coordinator,
        )
        from ...app_operator.coordinator import (
            create_app_operator_coordinator,
        )

        supervisor = create_k8sAutopilotSupervisorAgent(
            coordinators=[
                create_helm_coordinator(config),
                create_k8s_operator_coordinator(config),
                create_app_operator_coordinator(config),
            ],
            config=config,
        )

    Usage (single coordinator)::

        supervisor = create_k8sAutopilotSupervisorAgent(
            coordinator=create_helm_coordinator(config),
        )
    """

    return k8sAutopilotSupervisorAgent(
        agents=agents,
        config=config,
        custom_config=custom_config,
        prompt_template=prompt_template,
        name=name,
        coordinator=coordinator,
        coordinators=coordinators,
    )
