"""
A2A Executor for the K8s Autopilot Agent.

Orchestrates the A2A protocol lifecycle:
  1. Extract query from incoming context (text or A2UI userAction)
  2. Resolve / create task
  3. Set up A2UI session (activation + schema negotiation)
  4. Wrap query as ``Command(resume=...)`` when resuming an interrupt
  5. Dispatch A2UI schema event to client (just-in-time)
  6. Stream agent responses → dispatch to response handlers

Three response types are handled:
  - **completed**       → artifact + final status
  - **input_required**  → HITL pause (``TaskState.input_required``)
  - **working**         → intermediate progress update

Reference: aws-orchestrator-agent/core/a2a_executor.py
"""

import asyncio
import inspect
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    Part,
    Message,
    Role,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.helpers import (
    new_text_message,
    new_message,
    new_task_from_user_message,
    new_text_part,
    new_data_part,
)
from a2a.utils.errors import A2AError
from langgraph.types import Command

# A2UI imports
from a2ui.a2a import (
    A2UI_EXTENSION_BASE_URI,
    create_a2ui_part,
)

A2UI_EXTENSION_URI = f"{A2UI_EXTENSION_BASE_URI}/v0.9"

from k8s_autopilot.core.agents.types import AgentResponse, BaseAgent
from k8s_autopilot.core.a2ui.surface_builder import (
    TALKOPS_CATALOG_ID,
    build_thought_block_surface,
    build_tool_execution_surface,
    build_plan_todo_surface,
    update_plan_todo_data,
    update_thought_block_data,
    update_tool_execution_data,
)
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("A2AExecutor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_agent_message(parts: Sequence[Part]) -> Message:
    """Create an agent message with a unique ``messageId``."""
    return Message(role=Role.ROLE_AGENT, parts=parts, message_id=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Stream Renderer — manages token-stream formatting state
# ---------------------------------------------------------------------------

class _StreamRenderer:
    """Stateful helper for the token stream inside ``_stream_agent``.

    Responsibilities:
      • Maintains a **stable ``message_id``** (UUID) so the A2A frontend
        concatenates all ``final=False`` chunks into a single chat bubble.
      • Tracks whether a ``<details>`` thinking-block is currently open
        and provides ``open_thinking()`` / ``close_thinking()`` to toggle it.
      • Provides a single ``emit(text)`` method that sends a ``TextPart``
        via the ``TaskUpdater``.

    Reference: aws-orchestrator-agent _StreamRenderer
    """

    _OPEN_TAG = "\n<details open>\n<summary><b>Show thinking</b></summary>\n\n"
    _CLOSE_TAG = "\n</details>\n\n"

    # Map internal node/source names to friendly display labels.
    _LABEL_MAP: dict[str, str] = {
        "model": "",
        "tools": "",
        "": "",
        "coordinator": "Supervisor",
        "supervisor": "Supervisor",
    }

    def __init__(self, updater: TaskUpdater, context_id: str, task_id: str) -> None:
        self._updater = updater
        self._ctx = context_id
        self._task = task_id
        self.message_id = str(uuid.uuid4())
        self._thinking_open = False
        self._current_agent: str = ""

    # ── public API ────────────────────────────────────────────────────

    async def emit_with_label(self, text: Any, meta: dict) -> None:
        """Emit text, injecting an agent label if the source changed.

        Large raw JSON blobs (tool output piped into the AI reasoning path)
        are suppressed — they add no value and make the stream unreadable.
        """
        agent = self._resolve_agent(meta)
        if agent and agent != self._current_agent:
            self._current_agent = agent
            await self.emit(f"\n\n**{agent}**\n\n")

        content = str(text) if text else ""
        # Suppress raw JSON blobs leaking into the AI text stream.
        stripped = content.strip()
        if len(stripped) > 300 and stripped.startswith("{") and stripped.endswith("}"):
            return
        if len(stripped) > 300 and stripped.startswith("[") and stripped.endswith("]"):
            return

        await self.emit(content)

    async def emit(self, text: Any) -> None:
        """Send a text chunk to the client using the stable message ID."""
        content = str(text) if text else ""
        if not content:
            return
        msg = Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text=content)],
            message_id=self.message_id,
            context_id=self._ctx,
            task_id=self._task,
        )
        await self._updater.update_status(TaskState.TASK_STATE_WORKING, msg)
        await asyncio.sleep(0)  # yield to EventConsumer

    async def open_thinking(self) -> None:
        """Open a ``<details>`` thinking block if not already open."""
        if not self._thinking_open:
            await self.emit(self._OPEN_TAG)
            self._thinking_open = True

    async def close_thinking(self) -> None:
        """Close the ``<details>`` thinking block if currently open."""
        if self._thinking_open:
            await self.emit(self._CLOSE_TAG)
            self._thinking_open = False

    @classmethod
    def _resolve_agent(cls, meta: dict) -> str:
        """Return a display-ready agent label, or '' to suppress.

        Strips UUID suffixes like 'Agent:697F0E55-...' → 'Agent'.
        """
        for key in ("agent_name", "node", "source"):
            raw = meta.get(key, "")
            if not raw:
                continue
            if ":" in raw:
                raw = raw.split(":")[0].strip()
            if raw in cls._LABEL_MAP:
                mapped = cls._LABEL_MAP[raw]
                if mapped:
                    return mapped
                continue
            return raw.replace("_", " ").title()
        return ""


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class A2AAutoPilotExecutor(AgentExecutor):
    """A2A protocol executor for K8s Autopilot.

    Responsibilities are split into small, testable methods:

    * **Query extraction** — ``_extract_query``, ``_extract_user_action``
    * **Task lifecycle**  — ``_resolve_task``, ``_setup_session``, ``_wrap_resume``
    * **Streaming loop**  — ``_stream_agent``
    * **Response dispatch** — ``_handle_completed``, ``_handle_input_required``,
      ``_handle_working``

    Reference: aws-orchestrator-agent A2AExecutor
    """

    def __init__(self, agent: BaseAgent) -> None:
        self.agent = agent

    # ── public entry points ───────────────────────────────────────────

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute the full A2A request lifecycle."""
        logger.info(f"Executing agent {self.agent.name}", extra={"agent_name": self.agent.name},)

        # 1. Extract query (text or A2UI userAction)
        query: Union[str, Command, None] = self._extract_query(context)

        # 2. Resolve or create task
        task = await self._resolve_task(context, event_queue)
        ctx_id = task.context_id

        # 3. A2UI activation check
        use_ui = self._try_activate_a2ui(context)
        logger.info("A2UI extension check", extra={"use_ui": use_ui, "agent_name": self.agent.name},)

        # 4. Wrap as resume command if returning from interrupt
        query = self._wrap_resume(task, query)

        # 5. Create updater and stream
        updater = TaskUpdater(event_queue, task.id, ctx_id)

        # 6. Stream agent → dispatch response handlers
        await self._stream_agent(query, task, updater, event_queue, ctx_id, use_ui)

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Cancel the current agent execution if possible."""
        return None  # type: ignore[return-value]

    # ── Step 1: Query extraction ──────────────────────────────────────

    def _try_activate_a2ui(self, context: RequestContext) -> bool:
        """Check whether A2UI should be activated for this request."""
        if hasattr(context, "extensions") and context.extensions:  # type: ignore[attr-defined]
            if A2UI_EXTENSION_URI in context.extensions:  # type: ignore[attr-defined]
                return True
        # Always activate A2UI natively in autopilot Dev-Loop execution
        return True

    def _extract_query(self, context: RequestContext) -> Optional[str]:
        """Extract the user's query from the request context.

        Checks for plain text first, then falls back to parsing
        A2UI ``userAction`` DataParts.
        """
        query = context.get_user_input()
        if query:
            logger.debug(f"User query: {query[:120]}", extra={"agent_name": self.agent.name},)
            return query

        # Try A2UI userAction
        if context.message and context.message.parts:
            action_query = self._extract_user_action(context.message.parts)
            if action_query:
                return action_query

        return query

    def _extract_user_action(self, parts: Sequence[Part]) -> Optional[str]:
        """Parse A2UI ``userAction`` from message DataParts.

        Handles both standard A2UI parts and raw DataParts.
        For ``hitl_response`` actions, extracts the decision context.
        """
        for part in parts:
            user_action = self._get_user_action_from_part(part)
            if user_action is None:
                continue

            query = self._resolve_action_to_query(user_action)
            logger.info("Extracted A2UI userAction", extra={"action": query[:200], "agent_name": self.agent.name},)
            return query

        return None

    @staticmethod
    def _get_user_action_from_part(part: Part) -> Optional[dict]:
        """Extract ``userAction`` dict from a Part, or None."""
        if part.HasField("data"):
            from google.protobuf.json_format import MessageToDict
            data = MessageToDict(part.data)
            if isinstance(data, dict):
                return data.get("userAction")
        return None

    @staticmethod
    def _resolve_action_to_query(user_action: Any) -> str:
        """Convert a ``userAction`` dict into a query string for the agent.

        For ``hitl_response`` actions, extracts context items into a flat dict.
        When the context has only a ``decision`` key (simple approve/reject),
        returns the decision string directly.
        When the context has additional keys (e.g., ``repository``, ``branch``),
        returns the full context as JSON.
        """
        if not isinstance(user_action, dict):
            return json.dumps(user_action)

        if user_action.get("name") != "hitl_response":
            return json.dumps(user_action)

        # Parse HITL context items into a flat dict
        ctx: Dict[str, Any] = {}
        for item in user_action.get("context", []):
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not key:
                continue
            val = item.get("value")
            if isinstance(val, dict):
                ctx[key] = (
                    val.get("literalString")
                    or val.get("valueString")
                    or val.get("literalNumber")
                    or val.get("literalBoolean")
                    or val.get("path")
                    or val
                )
            else:
                ctx[key] = val

        decision = ctx.get("decision", "").strip()

        # Parse formInputs (from A2UI / Google Chat standard)
        form_inputs = user_action.get("formInputs", {})
        if form_inputs:
            for k, v in form_inputs.items():
                if isinstance(v, dict) and "stringInputs" in v:
                    strings = v["stringInputs"].get("value", [])
                    if strings:
                        ctx[k] = strings[0]
                elif isinstance(v, str):
                    ctx[k] = v

        # If context has additional data beyond decision, return full context
        non_decision_keys = {k for k in ctx if k != "decision" and ctx[k]}
        if decision and non_decision_keys:
            return json.dumps(ctx)

        # Simple decision-only context → return bare string (backward compat)
        if decision:
            return decision
        if ctx:
            return json.dumps(ctx)
        return json.dumps(user_action)

    # ── Step 2: Task resolution ───────────────────────────────────────

    @staticmethod
    async def _resolve_task(
        context: RequestContext,
        event_queue: EventQueue,
    ) -> Task:
        """Return the existing task or create a new one."""
        task = context.current_task
        if task:
            return task

        if context.message is None:
            raise A2AError(message="No message provided in request context.")

        task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        return task

    # ── Step 3: Resume wrapping ───────────────────────────────────────

    @staticmethod
    def _wrap_resume(task: Task, query: Any) -> Union[str, Command, None]:
        """Wrap ``query`` in ``Command(resume=...)`` if the task is paused."""
        if not (task and hasattr(task, "status") and hasattr(task.status, "state")):
            return query

        if task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            logger.info("Resuming from input_required — wrapping as Command(resume=...)",
                extra={
                    "task_id": task.id,
                    "preview": str(query)[:100] if query else "None",
                },
            )
            return Command(resume=query)

        return query

    # ── Step 4: Agent streaming ───────────────────────────────────────

    async def _stream_agent(
        self,
        query: Union[str, Command, None],
        task: Task,
        updater: TaskUpdater,
        event_queue: EventQueue,
        context_id: str,
        use_ui: bool,
    ) -> None:
        """Stream agent responses and dispatch to the appropriate handler.

        Uses a ``_StreamRenderer`` helper to manage thinking-block state
        and agent-transition labels, keeping this method focused on the
        dispatch logic only.
        """
        logger.info("Starting agent stream", extra={
                "agent_name": self.agent.name,
                "task_id": task.id,
                "context_id": context_id,
                "is_resume": isinstance(query, Command),
            },
        )

        renderer: Optional[_StreamRenderer] = None
        # ── Trace metadata for reasoning panel ────────────────────────
        _run_id = str(uuid.uuid4())
        _step_index = 0
        # ── Active surface trackers ───────────────────────────────────
        _active_tool_surfaces: Dict[str, Dict[str, Any]] = {}
        _reasoning_surface_id: Optional[str] = None
        _reasoning_buffer: str = ""
        _plan_todo_surface_id: Optional[str] = None

        try:
            stream_query: Union[str, Command] = (
                query if isinstance(query, Command) else str(query or "")
            )
            agent_stream = self.agent.stream(  # type: ignore[arg-type]
                stream_query,
                context_id,
                task.id,
                use_ui=use_ui,
            )
            if not inspect.isasyncgen(agent_stream):
                agent_stream = await agent_stream  # type: ignore[misc]

            renderer = _StreamRenderer(updater, context_id, task.id)

            async for item in agent_stream:  # type: ignore[union-attr]
                # ── Forward A2A inter-agent events directly ───────────
                if isinstance(item, StreamResponse):
                    payload_field = item.WhichOneof("payload")
                    if payload_field in ("status_update", "artifact_update"):
                        await event_queue.enqueue_event(getattr(item, payload_field))
                    continue

                # ── Classify the item ─────────────────────────────────
                is_complete = getattr(item, "is_task_complete", False)
                needs_input = getattr(item, "require_user_input", False)
                meta = getattr(item, "metadata", None) or {}
                message_type = meta.get("message_type", "")
                is_tool = (
                    getattr(item, "response_type", "") == "token"
                    and message_type in (
                        "tool_call", "tool_result",
                        "tool_started",
                    )
                )
                is_delegation = (
                    getattr(item, "response_type", "") == "token"
                    and message_type == "delegation"
                )

                # ── Terminal events ───────────────────────────────────
                # Before leaving the loop, close any tool surfaces that
                # never received a tool_result (stuck on "Running").
                async def _flush_active_tool_surfaces() -> None:
                    nonlocal _step_index
                    for tname, tracked in list(_active_tool_surfaces.items()):
                        duration = int(
                            (time.monotonic() - tracked["start_time"]) * 1000,
                        )
                        update_op = update_tool_execution_data(
                            tracked["surface_id"],
                            toolName=tracked["tool_name"],
                            parameters=tracked["parameters"],
                            environment=tracked["environment"],
                            status="error",
                            terminalOutput="Tool did not return a result.",
                            durationMs=duration,
                        )
                        parts = [create_a2ui_part(update_op)]
                        msg = self._make_stream_message_parts(
                            parts, renderer.message_id, context_id, task.id,
                        )
                        self._attach_trace_metadata(
                            msg, _run_id, _step_index, "tool_result",
                        )
                        await updater.update_status(
                            TaskState.TASK_STATE_WORKING, msg,
                        )
                        _step_index += 1
                    _active_tool_surfaces.clear()

                if is_complete:
                    await renderer.close_thinking()
                    _reasoning_surface_id = None
                    _reasoning_buffer = ""
                    await _flush_active_tool_surfaces()
                    await self._handle_completed(item, updater, task, context_id, use_ui, renderer.message_id)
                    return

                if needs_input:
                    await renderer.close_thinking()
                    _reasoning_surface_id = None
                    _reasoning_buffer = ""
                    await _flush_active_tool_surfaces()
                    await self._handle_input_required(item, updater, task, context_id, use_ui, renderer.message_id)
                    break

                # ── Tool progress → emit toolExecutionCard surfaces ───
                # Skip interrupt tools (request_human_feedback) — they
                # are handled by the HITL surface path instead.
                _INTERRUPT_TOOLS = {
                    "request_human_feedback",
                    "request_user_input",
                    "request_chat_continue",
                }

                # ── Delegation event → inline text label ──────────────
                if is_delegation:
                    # Close any open thinking block before delegation
                    await renderer.close_thinking()
                    if _reasoning_surface_id:
                        _reasoning_surface_id = None
                        _reasoning_buffer = ""

                    # Emit delegation label as inline text
                    await renderer.emit(str(item.content or ""))
                    continue

                if is_tool and use_ui:
                    tool_name = meta.get("tool_name", "unknown")

                    # ── Close reasoning surface at tool boundary ──────
                    # Each reasoning phase gets its own thoughtBlock.
                    # When a tool event arrives, the current reasoning
                    # block is complete — reset so the next reasoning
                    # tokens create a new block.
                    if _reasoning_surface_id:
                        _reasoning_surface_id = None
                        _reasoning_buffer = ""
                    await renderer.close_thinking()

                    if tool_name not in _INTERRUPT_TOOLS:
                        # ── write_todos → emit planTodoList surface ────
                        if tool_name == "write_todos":
                            todos_data = meta.get("tool_args") or {}
                            todos_list: list = []
                            # Extract todos from various formats
                            if isinstance(todos_data, dict):
                                todos_list = todos_data.get("todos", [])
                            elif isinstance(todos_data, list):
                                todos_list = todos_data

                            if todos_list:
                                if not _plan_todo_surface_id:
                                    # First write_todos call → create surface
                                    _plan_todo_surface_id = f"plan-todo-{uuid.uuid4().hex[:8]}"
                                    ops = build_plan_todo_surface(
                                        surface_id=_plan_todo_surface_id,
                                        todos=todos_list,
                                        plan_title="Execution Plan",
                                        plan_version=1,
                                    )
                                    for op in ops:
                                        parts = [create_a2ui_part(op)]
                                        msg = self._make_stream_message_parts(
                                            parts, renderer.message_id, context_id, task.id,
                                        )
                                        self._attach_trace_metadata(
                                            msg, _run_id, _step_index, "plan_todo",
                                        )
                                        await updater.update_status(
                                            TaskState.TASK_STATE_WORKING, msg,
                                        )
                                        _step_index += 1
                                        await asyncio.sleep(0)
                                else:
                                    # Subsequent write_todos → update data
                                    update_op = update_plan_todo_data(
                                        _plan_todo_surface_id,
                                        todos=todos_list,
                                    )
                                    parts = [create_a2ui_part(update_op)]
                                    msg = self._make_stream_message_parts(
                                        parts, renderer.message_id, context_id, task.id,
                                    )
                                    self._attach_trace_metadata(
                                        msg, _run_id, _step_index, "plan_todo_update",
                                    )
                                    await updater.update_status(
                                        TaskState.TASK_STATE_WORKING, msg,
                                    )
                                    _step_index += 1
                                    await asyncio.sleep(0)

                            # Skip the standard toolExecutionCard for write_todos
                            continue
                        if message_type == "tool_call":
                            # Create a new toolExecutionCard surface.
                            # Use humanized name from metadata if available.
                            display_name = str(meta.get("tool_display_name") or tool_name)
                            surface_id = f"tool-{tool_name}-{uuid.uuid4().hex[:8]}"
                            ops = build_tool_execution_surface(
                                surface_id=surface_id,
                                tool_name=display_name,
                                status="running",
                                parameters=meta.get("tool_args"),
                                environment=meta.get("environment", ""),
                            )
                            for op in ops:
                                parts = [create_a2ui_part(op)]
                                msg = self._make_stream_message_parts(
                                    parts, renderer.message_id, context_id, task.id,
                                )
                                self._attach_trace_metadata(
                                    msg, _run_id, _step_index, "tool_call",
                                )
                                await updater.update_status(
                                    TaskState.TASK_STATE_WORKING, msg,
                                )
                                _step_index += 1
                                await asyncio.sleep(0)

                            _active_tool_surfaces[tool_name] = {
                                "surface_id": surface_id,
                                "start_time": time.monotonic(),
                                "tool_name": tool_name,
                                "parameters": meta.get("tool_args") or {},
                                "environment": meta.get("environment", ""),
                            }

                        elif message_type == "tool_started":
                            # tool_started events are for sub-tools inside
                            # coordinators. Create a toolExecutionCard for them
                            # so they appear as separate cards in the timeline.
                            display_name = str(meta.get("tool_display_name") or tool_name)
                            surface_id = f"tool-{tool_name}-{uuid.uuid4().hex[:8]}"
                            ops = build_tool_execution_surface(
                                surface_id=surface_id,
                                tool_name=display_name,
                                status="running",
                                parameters=meta.get("tool_args"),
                                environment=meta.get("environment", ""),
                            )
                            for op in ops:
                                parts = [create_a2ui_part(op)]
                                msg = self._make_stream_message_parts(
                                    parts, renderer.message_id, context_id, task.id,
                                )
                                self._attach_trace_metadata(
                                    msg, _run_id, _step_index, "tool_started",
                                )
                                await updater.update_status(
                                    TaskState.TASK_STATE_WORKING, msg,
                                )
                                _step_index += 1
                                await asyncio.sleep(0)

                            _active_tool_surfaces[tool_name] = {
                                "surface_id": surface_id,
                                "start_time": time.monotonic(),
                                "tool_name": display_name,
                                "parameters": meta.get("tool_args") or {},
                                "environment": meta.get("environment", ""),
                            }

                        elif message_type == "tool_result":
                            # Update existing toolExecutionCard surface.
                            # IMPORTANT: A2UI updateDataModel REPLACES the
                            # root, so we must re-send ALL fields.
                            tracked = _active_tool_surfaces.pop(tool_name, None)
                            if tracked:
                                duration = int(
                                    (time.monotonic() - tracked["start_time"]) * 1000,
                                )
                                is_error = meta.get("is_error", False)
                                output_text = str(item.content or "")[:2000]
                                update_op = update_tool_execution_data(
                                    tracked["surface_id"],
                                    toolName=tracked["tool_name"],
                                    parameters=tracked["parameters"],
                                    environment=tracked["environment"],
                                    status="error" if is_error else "success",
                                    terminalOutput=output_text,
                                    durationMs=duration,
                                )
                                parts = [create_a2ui_part(update_op)]
                                msg = self._make_stream_message_parts(
                                    parts, renderer.message_id, context_id, task.id,
                                )
                                self._attach_trace_metadata(
                                    msg, _run_id, _step_index, "tool_result",
                                )
                                await updater.update_status(
                                    TaskState.TASK_STATE_WORKING, msg,
                                )
                                _step_index += 1
                                await asyncio.sleep(0)

                        # Tool surfaces handle the visual rendering; skip
                        # emitting the same content into the v2-stream text.
                        continue

                # Fallback: tool events without UI → existing behavior
                if is_tool:
                    await renderer.open_thinking()
                    await renderer.emit_with_label(item.content, meta)
                    continue

                # ── Reasoning token → emit thoughtBlock surface ───────
                is_reasoning = (
                    getattr(item, "response_type", "") == "token"
                    and message_type == "reasoning"
                )
                if is_reasoning:
                    if use_ui:
                        _reasoning_buffer += str(item.content or "")

                        # Derive a context-appropriate title for the
                        # thoughtBlock. Use the source agent name when
                        # available (e.g. "transfer_to_helm_operator" →
                        # "Helm Operator"), otherwise fall back to the
                        # generic resolver.
                        source = meta.get("source", "")
                        if source and source.startswith("transfer_to_"):
                            agent_label = (
                                source.replace("transfer_to_", "")
                                .replace("_", " ").title()
                            )
                        else:
                            agent_label = renderer._resolve_agent(meta) or "Reasoning"

                        if not _reasoning_surface_id:
                            # Create a new thoughtBlock surface
                            _reasoning_surface_id = f"thought-{uuid.uuid4().hex[:8]}"
                            ops = build_thought_block_surface(
                                surface_id=_reasoning_surface_id,
                                title=agent_label,
                                summary=_reasoning_buffer[-500:],
                                severity="info",
                            )
                            for op in ops:
                                parts = [create_a2ui_part(op)]
                                msg = self._make_stream_message_parts(
                                    parts, renderer.message_id, context_id, task.id,
                                )
                                self._attach_trace_metadata(
                                    msg, _run_id, _step_index, "reasoning",
                                )
                                await updater.update_status(
                                    TaskState.TASK_STATE_WORKING, msg,
                                )
                                _step_index += 1
                                await asyncio.sleep(0)
                        else:
                            # Update existing thoughtBlock data
                            update_op = update_thought_block_data(
                                _reasoning_surface_id,
                                summary=_reasoning_buffer[-500:],
                            )
                            parts = [create_a2ui_part(update_op)]
                            msg = self._make_stream_message_parts(
                                parts, renderer.message_id, context_id, task.id,
                            )
                            self._attach_trace_metadata(
                                msg, _run_id, _step_index, "reasoning",
                            )
                            await updater.update_status(
                                TaskState.TASK_STATE_WORKING, msg,
                            )
                            _step_index += 1
                            await asyncio.sleep(0)

                    # Also emit via renderer for text stream
                    await renderer.open_thinking()
                    await renderer.emit_with_label(item.content, meta)
                    continue

                # ── AI text token → outside thinking block ────────────
                if getattr(item, "response_type", "") == "token":
                    await renderer.close_thinking()
                    # Close reasoning surface when text tokens start
                    if _reasoning_surface_id:
                        _reasoning_surface_id = None
                        _reasoning_buffer = ""
                    await renderer.emit_with_label(item.content, meta)
                    continue

                # ── Structured / other working updates ────────────────
                await renderer.close_thinking()
                await self._handle_working(item, updater, task, context_id, use_ui, renderer.message_id)

        except asyncio.CancelledError:
            logger.warning(
                "Task was cancelled by user.",
                extra={"task_id": task.id, "agent_name": self.agent.name},
            )
            msg_id = renderer.message_id if renderer else None
            await updater.update_status(
                TaskState.TASK_STATE_CANCELED,
                self._make_stream_message_text(
                    "Task canceled.",
                    msg_id,
                    context_id,
                    task.id,
                ),
            )
            await self._safe_complete(updater, task)
            raise
        except Exception as e:
            logger.error(f"Exception in agent stream: {e}", extra={"agent_name": self.agent.name, "task_id": task.id},)
            raise

    # ── Response handlers ─────────────────────────────────────────────

    async def _handle_completed(
        self,
        item: AgentResponse,
        updater: TaskUpdater,
        task: Task,
        context_id: str,
        use_ui: bool,
        stream_message_id: str = "",
    ) -> None:
        """Handle a **completed** response from the agent."""
        logger.info("Task marked complete by agent", extra={"task_id": task.id, "agent_name": self.agent.name},)

        if use_ui:
            if item.response_type == "token":
                # Close the stream directly with an empty final chunk
                await updater.update_status(
                    TaskState.TASK_STATE_COMPLETED,
                    self._make_stream_message_text("", stream_message_id, context_id, task.id),
                )
            else:
                content = item.content or "Task completed successfully."
                if isinstance(content, str) and not content.strip():
                    content = "Task completed successfully."

                parts = self._build_a2ui_parts(
                    content=content,
                    status="completed",
                    is_task_complete=True,
                    response_type=item.response_type,
                    metadata=item.metadata,
                    use_ui=use_ui,
                    session_id=context_id,
                    task_id=task.id,
                )
                await updater.add_artifact(
                    parts, name=f"{self.agent.name}-result"
                )
                await updater.update_status(
                    TaskState.TASK_STATE_COMPLETED,
                    self._make_stream_message_parts(parts, stream_message_id, context_id, task.id),
                )
        else:
            # Plain text / data fallback
            if item.response_type == "data":
                part: Part = new_data_part(data=item.content)
            else:
                part = new_text_part(text=self._content_to_str(item.content))
            await updater.add_artifact([part], name=f"{self.agent.name}-result")
            await updater.update_status(
                TaskState.TASK_STATE_COMPLETED,
                self._make_stream_message_text("Task completed successfully.", stream_message_id, context_id, task.id),
            )

        await self._safe_complete(updater, task)

    async def _handle_input_required(
        self,
        item: AgentResponse,
        updater: TaskUpdater,
        task: Task,
        context_id: str,
        use_ui: bool,
        stream_message_id: str = "",
    ) -> None:
        """Handle an **input_required** response (HITL interrupt)."""
        logger.info("Agent requires user input", extra={"task_id": task.id, "agent_name": self.agent.name},)

        if use_ui:
            if item.response_type == "token":
                await updater.update_status(
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    self._make_stream_message_text("", stream_message_id, context_id, task.id),
                )
            else:
                parts = self._build_a2ui_parts(
                    content=item.content,
                    status="input_required",
                    is_task_complete=False,
                    require_user_input=True,
                    response_type=item.response_type,
                    metadata=item.metadata,
                    use_ui=use_ui,
                    session_id=context_id,
                    task_id=task.id,
                )
                await updater.update_status(
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    self._make_stream_message_parts(parts, stream_message_id, context_id, task.id),
                )
        else:
            text = (
                "Please provide input."
                if item.response_type == "token"
                else self._content_to_str(item.content)
            )
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                self._make_stream_message_text(text, stream_message_id, context_id, task.id),
            )

    async def _handle_working(
        self,
        item: AgentResponse,
        updater: TaskUpdater,
        task: Task,
        context_id: str,
        use_ui: bool,
        stream_message_id: Optional[str] = None,
    ) -> None:
        """Handle an intermediate **working** update.

        Token stream: lightweight TextPart via update_status only.
        All other updates: full A2UI artifact path when use_ui is active.
        """
        if use_ui and item.response_type != "token":
            parts = self._build_a2ui_parts(
                content=item.content,
                status="working",
                is_task_complete=False,
                response_type=item.response_type,
                metadata=item.metadata,
            )
            await updater.add_artifact(parts, name=f"{self.agent.name}-intermediate")
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_message(parts, context_id=context_id, task_id=task.id, role=Role.ROLE_AGENT),
            )
        else:
            text = self._content_to_str(item.content)
            msg = self._make_stream_message_text(text, stream_message_id, context_id, task.id)
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                msg,
            )
        await asyncio.sleep(0)  # yield control to EventConsumer

    # ── Shared utilities ──────────────────────────────────────────────

    def _build_a2ui_parts(
        self,
        content: Any,
        status: str = "working",
        is_task_complete: bool = False,
        require_user_input: bool = False,
        response_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
        use_ui: bool = True,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> List[Part]:
        """Build A2UI Parts using the programmatic registry."""
        from k8s_autopilot.core.a2ui.registry import get_registry, RenderContext
        
        meta = metadata or {}
        if "status" not in meta:
            meta["status"] = status
            
        ctx = RenderContext(
            content=content,
            status=meta.get("status", status),
            response_type=response_type,
            is_task_complete=is_task_complete,
            require_user_input=require_user_input,
            phase_override=meta.get("phase"),
            agent_name=self.agent.name,
            metadata=meta,
            use_ui=use_ui,
            session_id=session_id,
            task_id=task_id,
        )
        
        # Now properly returns List[Part] wrapped strictly via the A2UI SDK
        return get_registry().build_parts(ctx)

    @staticmethod
    def _content_to_str(content: Any) -> str:
        """Convert content to a display-friendly string."""
        if isinstance(content, dict):
            return (
                content.get("summary")
                or content.get("question")
                or content.get("message")
                or json.dumps(content, indent=2)
            )
        return str(content) if content else "Processing..."

    def _attach_trace_metadata(
        self,
        msg: Message,
        run_id: str,
        step_index: int,
        event_type: str,
    ) -> None:
        """Attach trace metadata to a ``Message`` for the reasoning panel.

        The metadata enables the UI-side reasoning panel to reconstruct
        the full execution trace with proper ordering and attribution.

        Args:
            msg: The Message to enrich (modified in-place).
            run_id: Stable UUID for this agent stream run.
            step_index: Monotonically increasing step counter.
            event_type: Event classification (``tool_call``, ``tool_result``,
                ``reasoning``, etc.).
        """
        msg.metadata.update({
            "traceRunId": run_id,
            "traceStepIndex": step_index,
            "agentName": self.agent.name,
            "eventType": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @staticmethod
    def _map_status(custom_status: str) -> TaskState:
        """Map custom status strings to ``TaskState`` enum values."""
        return {
            "working": TaskState.TASK_STATE_WORKING,
            "input_required": TaskState.TASK_STATE_INPUT_REQUIRED,
            "completed": TaskState.TASK_STATE_COMPLETED,
            "failed": TaskState.TASK_STATE_FAILED,
            "error": TaskState.TASK_STATE_FAILED,
            "submitted": TaskState.TASK_STATE_SUBMITTED,
        }.get(custom_status, TaskState.TASK_STATE_WORKING)

    def _make_stream_message_text(
        self, text: str, message_id: Optional[str], context_id: str, task_id: str
    ) -> Message:
        """Create a plain text streaming message with a stable ID."""
        if not message_id:
            return new_text_message(text, context_id=context_id, task_id=task_id, role=Role.ROLE_AGENT)
        return Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text=text)],
            message_id=message_id,
            context_id=context_id,
            task_id=task_id,
        )

    def _make_stream_message_parts(
        self, parts: Sequence[Part], message_id: Optional[str], context_id: str, task_id: str
    ) -> Message:
        """Create a parts-based message with a stable ID."""
        if not message_id:
            return new_message(list(parts), context_id=context_id, task_id=task_id, role=Role.ROLE_AGENT)
        return Message(
            role=Role.ROLE_AGENT,
            parts=list(parts),
            message_id=message_id,
            context_id=context_id,
            task_id=task_id,
        )

    @staticmethod
    async def _safe_complete(updater: TaskUpdater, task: Task) -> None:
        """Call ``updater.complete()`` with graceful handling of terminal-state errors."""
        try:
            await updater.complete()
            logger.info("Task completed", extra={"task_id": task.id},)
        except RuntimeError as e:
            if "already in a terminal state" in str(e):
                logger.info("Task already terminal, skipping complete()",
                    extra={"task_id": task.id},
                )
            else:
                raise