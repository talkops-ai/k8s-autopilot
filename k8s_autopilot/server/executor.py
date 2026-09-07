"""
A2A Executor for the K8s Autopilot Agent.

Orchestrates the A2A protocol lifecycle:
  1. Extract query from incoming context (text or A2UI userAction)
  2. Resolve / create task
  3. Set up A2UI session (activation + schema negotiation)
  4. Wrap query as ``Command(resume=...)`` when resuming an interrupt
  5. Stream LangGraph Pregel events → dispatch to A2A / A2UI handlers

Events handled during streaming:
  - **AIMessageChunk**: text tokens, thinking/reasoning traces, tool calls
  - **ToolMessage**: tool results, duration, embedded A2UI surfaces
  - **__interrupt__**: HITL tool approvals, ask_user question prompts
  - **Custom events**: subagents, rubrics, auto-mode decisions
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from a2a.helpers import (
    new_data_part,
    new_message,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    Message,
    Part,
    Role,
    StreamResponse,
    Task,
    TaskState,
)
from a2a.utils.errors import A2AError

# A2UI imports
A2UI_EXTENSION_BASE_URI = "https://a2ui.org/a2a-extension/a2ui"
try:
    from a2ui.a2a import (
        A2UI_EXTENSION_BASE_URI,
        create_a2ui_part,
    )
except ImportError:
    from google.protobuf import json_format, struct_pb2  # type: ignore[import-untyped]

    def create_a2ui_part(a2ui_data: dict) -> Part:
        val_cls = getattr(struct_pb2, "Value", None)
        val = val_cls() if val_cls else None
        parsed = json_format.ParseDict(a2ui_data, val) if val is not None else a2ui_data
        return Part(
            data=parsed,
            metadata={"mimeType": "application/json+a2ui"},
        )
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command

from k8s_autopilot.a2ui.surface_builder import (
    build_ask_user_surface,
    build_goal_confirmation_surface,
    build_hitl_approval_surface,
    build_plan_todo_surface,
    build_thought_block_surface,
    build_tool_execution_surface,
    build_walkthrough_surface,
    update_ask_user_data,
    update_goal_confirmation_data,
    update_plan_todo_data,
    update_thought_block_data,
    update_tool_execution_data,
    update_walkthrough_data,
)

from k8s_autopilot.integrations.stream_bridge import (
    _extract_text_and_thinking,
    _humanize_tool_name,
)
from k8s_autopilot.middleware.goal_state_notice import is_conversation_control_message
from k8s_autopilot.utils.logger import get_logger

A2UI_EXTENSION_URI = f"{A2UI_EXTENSION_BASE_URI}/v0.9"

logger = get_logger(__name__)


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
        self._response_buffer: list[str] = []
        self._attach_trace: Any = None

    def set_attach_trace(self, fn: Any) -> None:
        """Register the trace metadata attach function for outgoing stream messages."""
        self._attach_trace = fn

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

        if not self._thinking_open and content.strip():
            self._response_buffer.append(content)

        msg = Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text=content)],
            message_id=self.message_id,
            context_id=self._ctx,
            task_id=self._task,
        )
        if callable(self._attach_trace):
            try:
                self._attach_trace(msg, "text")
            except Exception:
                pass
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

    def get_accumulated_response(self) -> str:
        """Return the accumulated non-thinking response text."""
        return "".join(self._response_buffer).strip()

    @classmethod
    def _resolve_agent(cls, meta: dict) -> str:
        """Return a display-ready agent label, or '' to suppress."""
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
# Stream State Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _StreamTelemetryState:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    step_index: int = 0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cost_usd: float = 0.0
    processed_message_tokens: dict[str, tuple[int, int]] = field(default_factory=dict)
    current_goal_status: str | None = None
    current_goal_objective: str | None = None
    current_rubric: str | None = None
    current_approval_mode: str = "manual"
    approval_mode_key: str | None = None
    active_model: str = ""
    active_effort: str = ""

    def derive_rubric_label(self) -> str | None:
        if self.current_goal_status == "complete":
            return "✓ Goal complete"
        if self.current_goal_status == "blocked":
            return "⚠ Goal blocked"
        if self.current_goal_status == "paused":
            return "⏸ Goal paused"
        if self.current_rubric and self.current_goal_status == "active":
            return "✓ Rubric set"
        if self.current_rubric:
            return "✓ Rubric set"
        if self.current_goal_status == "active":
            return "Active"
        return None


@dataclass
class _StreamContext:
    task: Task
    updater: TaskUpdater
    event_queue: EventQueue
    context_id: str
    use_ui: bool
    renderer: _StreamRenderer
    telemetry: _StreamTelemetryState
    active_tool_surfaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    reasoning_surface_id: str | None = None
    reasoning_buffer: str = ""
    plan_todo_surface_id: str | None = None
    subagent_reasoning_surfaces: dict[str, str] = field(default_factory=dict)
    subagent_reasoning_buffers: dict[str, str] = field(default_factory=dict)
    interrupted: bool = False


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class A2AAutoPilotExecutor(AgentExecutor):
    """A2A protocol executor for K8s Autopilot Agent.

    Responsibilities:
    * **Query extraction** — ``_extract_query``, ``_extract_user_action``
    * **Task lifecycle**  — ``_resolve_task``, ``_wrap_resume``
    * **Streaming loop**  — ``_stream_agent`` (drives LangGraph Pregel stream)
    * **A2UI surface dispatch** — creates and updates interactive cards
    """

    _instances: list["A2AAutoPilotExecutor"] = []

    def __init__(self, agent: Any = None, checkpointer: Any = None) -> None:
        self.agent = agent
        self.checkpointer = checkpointer
        self._custom_agent = agent is not None
        self._active_model: str | None = None
        self._active_effort: str | None = None
        self._active_checkpointer: Any = None
        self._active_mcp_fingerprint: str | None = None
        A2AAutoPilotExecutor._instances.append(self)
        if self.agent is not None and not hasattr(self.agent, "name"):
            try:
                self.agent.name = "k8sAutopilotAgent"
            except (AttributeError, TypeError):
                pass

    @classmethod
    def invalidate_all_agents(cls) -> None:
        """Clear cached agent graphs on all executor instances to force rebuild on next request."""
        for inst in cls._instances:
            inst.agent = None
            inst._active_model = None
            inst._active_effort = None
            inst._active_checkpointer = None
            inst._active_mcp_fingerprint = None

    def _extract_model_and_effort(self, context: RequestContext) -> tuple[str | None, str | None]:
        """Extract user-selected model spec and reasoning effort from RequestContext if present."""
        req_model: str | None = None
        req_effort: str | None = None

        def _extract_from_obj(obj: Any) -> tuple[str | None, str | None]:
            if obj is None:
                return None, None
            if isinstance(obj, dict):
                m = obj.get("model") or obj.get("model_spec")
                e = obj.get("reasoning_effort") or obj.get("effort")
                return (str(m) if m is not None else None, str(e) if e is not None else None)
            if hasattr(obj, "fields"):
                from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

                try:
                    d = MessageToDict(obj)
                    if isinstance(d, dict):
                        m = d.get("model") or d.get("model_spec")
                        e = d.get("reasoning_effort") or d.get("effort")
                        return (str(m) if m is not None else None, str(e) if e is not None else None)
                except Exception:
                    pass
            get_fn = getattr(obj, "get", None)
            if callable(get_fn):
                try:
                    m = get_fn("model") or get_fn("model_spec")
                    e = get_fn("reasoning_effort") or get_fn("effort")
                    return (str(m) if m is not None else None, str(e) if e is not None else None)
                except Exception:
                    pass
            m = getattr(obj, "model", None) or getattr(obj, "model_spec", None)
            e = getattr(obj, "reasoning_effort", None) or getattr(obj, "effort", None)
            return (str(m) if m is not None else None, str(e) if e is not None else None)

        # 1. Check configuration
        config = getattr(context, "configuration", None)
        if config:
            req_model, req_effort = _extract_from_obj(config)

        # 2. Check message metadata
        if not req_model:
            msg = getattr(context, "message", None)
            meta = getattr(msg, "metadata", None) if msg else None
            if meta:
                m, e = _extract_from_obj(meta)
                req_model = req_model or m
                req_effort = req_effort or e

        # 3. Check request-level metadata
        if not req_model:
            meta = getattr(context, "metadata", None)
            if meta:
                m, e = _extract_from_obj(meta)
                req_model = req_model or m
                req_effort = req_effort or e

        return req_model, req_effort

    def _extract_approval_mode(self, context: RequestContext) -> str | None:
        """Extract user-selected approval mode from RequestContext if present."""
        def _extract_mode_from_obj(obj: Any) -> str | None:
            if obj is None:
                return None
            if isinstance(obj, dict):
                m = obj.get("approval_mode") or obj.get("approvalMode") or obj.get("mode")
                if m and str(m).strip().lower() in ("manual", "auto", "yolo"):
                    return str(m).strip().lower()
                return None
            if hasattr(obj, "fields"):
                from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

                try:
                    d = MessageToDict(obj)
                    if isinstance(d, dict):
                        m = d.get("approval_mode") or d.get("approvalMode") or d.get("mode")
                        if m and str(m).strip().lower() in ("manual", "auto", "yolo"):
                            return str(m).strip().lower()
                except Exception:
                    pass
            get_fn = getattr(obj, "get", None)
            if callable(get_fn):
                try:
                    m = get_fn("approval_mode") or get_fn("approvalMode") or get_fn("mode")
                    if m and str(m).strip().lower() in ("manual", "auto", "yolo"):
                        return str(m).strip().lower()
                except Exception:
                    pass
            m = getattr(obj, "approval_mode", None) or getattr(obj, "approvalMode", None)
            if m and str(m).strip().lower() in ("manual", "auto", "yolo"):
                return str(m).strip().lower()
            return None

        # 1. Check configuration
        config = getattr(context, "configuration", None)
        if config:
            mode = _extract_mode_from_obj(config)
            if mode:
                return mode

        # 2. Check message metadata
        msg = getattr(context, "message", None)
        meta = getattr(msg, "metadata", None) if msg else None
        if meta:
            mode = _extract_mode_from_obj(meta)
            if mode:
                return mode

        # 3. Check request-level metadata
        req_meta = getattr(context, "metadata", None)
        if req_meta:
            mode = _extract_mode_from_obj(req_meta)
            if mode:
                return mode

        return None

    def _compute_mcp_fingerprint(self) -> str:
        """Compute a deterministic fingerprint of currently enabled MCP server configurations."""
        try:
            from k8s_autopilot.mcp.discovery import discover_mcp_configs

            configs = discover_mcp_configs()
            items = []
            for name in sorted(configs.keys()):
                c = configs[name]
                items.append((
                    name,
                    c.get("command"),
                    tuple(c.get("args") or ()),
                    c.get("url"),
                    c.get("transport"),
                    c.get("enabled", True),
                    tuple(sorted(c.get("disabled_tools") or ())),
                    tuple(sorted(c.get("allowed_tools") or ())),
                ))
            return str(items)
        except Exception as exc:
            logger.debug("Could not compute MCP fingerprint: %s", exc)
            return ""

    def _ensure_agent(
        self,
        requested_model: str | None = None,
        requested_effort: str | None = None,
    ) -> Any:
        if self._custom_agent and self.agent is not None:
            return self.agent
        from k8s_autopilot.config.settings import get_settings

        settings = get_settings()
        active_model = requested_model or settings.model or settings.model_name
        current_model = getattr(self, "_active_model", None)

        extra_kwargs: dict[str, Any] = {}
        effort = requested_effort or getattr(settings, "reasoning_effort", None)
        if effort:
            extra_kwargs["reasoning_effort"] = effort
        current_effort = getattr(self, "_active_effort", None)
        current_checkpointer = getattr(self, "checkpointer", None)
        active_checkpointer = getattr(self, "_active_checkpointer", None)

        current_mcp_fingerprint = getattr(self, "_active_mcp_fingerprint", None)
        active_mcp_fingerprint = self._compute_mcp_fingerprint()

        if (
            self.agent is None
            or (active_model and active_model != current_model)
            or (effort and effort != current_effort)
            or (current_checkpointer is not None and current_checkpointer != active_checkpointer)
            or (active_mcp_fingerprint != current_mcp_fingerprint)
        ):
            from langgraph.checkpoint.memory import MemorySaver

            from k8s_autopilot.agent import create_k8s_autopilot_agent

            if self.checkpointer is None:
                self.checkpointer = MemorySaver()
            current_checkpointer = self.checkpointer

            if active_mcp_fingerprint != current_mcp_fingerprint and self.agent is not None:
                logger.info(
                    "Active MCP configuration changed; recompiling agent graph with latest toolset"
                )

            self.agent, _ = create_k8s_autopilot_agent(
                model=active_model,
                reasoning_effort=effort,
                extra_kwargs=extra_kwargs if extra_kwargs else None,
                checkpointer=current_checkpointer,
                auto_approve=(settings.approval_mode == "yolo"),
            )
            self._active_model = active_model
            self._active_effort = effort
            self._active_checkpointer = current_checkpointer
            self._active_mcp_fingerprint = active_mcp_fingerprint
            if not hasattr(self.agent, "name"):
                try:
                    self.agent.name = "k8sAutopilotAgent"
                except (AttributeError, TypeError):
                    pass
        return self.agent

    _get_or_create_agent = _ensure_agent

    # ── public entry points ───────────────────────────────────────────

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute the full A2A request lifecycle."""
        req_model, req_effort = self._extract_model_and_effort(context)
        req_mode = self._extract_approval_mode(context)
        self._ensure_agent(requested_model=req_model, requested_effort=req_effort)
        agent_name = getattr(self.agent, "name", "k8sAutopilotAgent")
        logger.info("Executing agent %s", agent_name, extra={"agent_name": agent_name})

        # 1. Extract query (text or A2UI userAction)
        query: str | Command | None = self._extract_query(context)

        # 2. Resolve or create task
        task = await self._resolve_task(context, event_queue)
        ctx_id = task.context_id

        # 2a. Guard against bare approval mode switch commands
        if isinstance(query, str):
            clean_cmd = query.strip().lower()
            if clean_cmd.startswith("/"):
                clean_cmd = clean_cmd[1:]
            if clean_cmd in ("auto", "manual", "yolo"):
                logger.info(
                    "Intercepted bare mode command '%s'; mode set to '%s'",
                    query,
                    clean_cmd,
                    context_id=ctx_id,
                    extra={"mode": clean_cmd},
                )
                target_mode = clean_cmd
                try:
                    from k8s_autopilot.security.approval_mode import awrite_approval_mode

                    await awrite_approval_mode(self.agent, ctx_id, mode=target_mode)
                except Exception as exc:
                    logger.debug("Failed to persist mode from bare command to store: %s", exc)

                # NOTE: ConfigStore DB write removed — mode is session-scoped
                # and flows via A2A metadata + LangGraph in-memory store only.

                from k8s_autopilot.config.settings import get_settings

                get_settings().approval_mode = target_mode

                updater = TaskUpdater(event_queue, task.id, ctx_id)
                msg_out = self._make_stream_message_text(
                    f"Switched approval mode to {target_mode.upper()}.",
                    None,
                    ctx_id,
                    task.id,
                )
                await updater.update_status(TaskState.TASK_STATE_COMPLETED, msg_out)
                await self._safe_complete(updater, task)
                return

        # 2b. Auto-create/touch conversation in the DB
        try:
            from k8s_autopilot.api.service import get_thread_service

            service = get_thread_service()
            if service:
                user_text = ""
                if isinstance(query, str):
                    user_text = query
                elif hasattr(query, "resume") and isinstance(getattr(query, "resume"), str):
                    user_text = getattr(query, "resume")
                await service.auto_touch(
                    ctx_id,
                    user_id="default",
                    agent_id=getattr(self.agent, "name", "k8sAutopilotAgent"),
                    user_query=user_text,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto-touch failed for %s: %s", ctx_id, exc, context_id=ctx_id)

        # 3. A2UI activation check
        use_ui = self._try_activate_a2ui(context)
        logger.info(
            "Inbound A2A request received",
            task_id=task.id,
            context_id=ctx_id,
            extra={
                "model": req_model,
                "approval_mode": req_mode,
                "use_ui": use_ui,
                "agent_name": agent_name,
                "is_resume": isinstance(query, Command),
            },
        )

        from k8s_autopilot.config.settings import get_settings

        settings = get_settings()
        active_model = req_model or settings.model or settings.model_name or "gemini-3.7-flash"
        active_effort = req_effort or getattr(settings, "reasoning_effort", None) or "medium"
        agent_graph = self._ensure_agent(
            requested_model=active_model,
            requested_effort=active_effort,
        )

        config: dict[str, Any] = {
            "configurable": {"thread_id": ctx_id},
            "run_name": f"k8s-autopilot:{task.id[:8]}",
            "tags": ["k8s-autopilot", f"thread:{ctx_id}"],
            "metadata": {
                "thread_id": ctx_id,
                "task_id": task.id,
                "session_id": ctx_id,
            },
        }

        # 4. Wrap as resume command if returning from interrupt
        query = await self._wrap_resume(agent_graph, config, task, query)

        # 5. Create updater and stream
        updater = TaskUpdater(event_queue, task.id, ctx_id)

        try:
            # 6. Stream agent graph → dispatch response handlers
            await self._stream_agent(
                query,
                task,
                updater,
                event_queue,
                ctx_id,
                use_ui,
                requested_model=active_model,
                requested_effort=active_effort,
                requested_mode=req_mode,
                agent_graph=agent_graph,
                config=config,
            )
        except asyncio.CancelledError:
            raise
        except GeneratorExit:
            logger.info("Execution stream closed for task %s", task.id, task_id=task.id, context_id=ctx_id)
            return
        except Exception as exc:
            logger.error(
                "Exception in agent execution stream",
                task_id=task.id,
                context_id=ctx_id,
                exc_info=True,
            )
            msg = self._make_stream_message_text(
                f"Agent execution encountered an error: {exc}",
                None,
                ctx_id,
                task.id,
            )
            try:
                await updater.update_status(TaskState.TASK_STATE_FAILED, msg)
                await self._safe_complete(updater, task)
            except Exception:
                pass


    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Cancel current agent execution if possible."""
        return

    # ── Step 1: Query extraction ──────────────────────────────────────

    def _try_activate_a2ui(self, context: RequestContext) -> bool:
        """Check whether A2UI should be activated for this request."""
        if hasattr(context, "extensions") and context.extensions:  # type: ignore[attr-defined]
            if A2UI_EXTENSION_URI in context.extensions:  # type: ignore[attr-defined]
                return True
        return True

    def _extract_query(self, context: RequestContext) -> str | None:
        """Extract user's query from request context."""
        query = context.get_user_input()
        if query:
            logger.debug(
                f"User text query extracted: {query[:120]}",
                extra={"agent_name": getattr(self.agent, "name", "k8sAutopilotAgent")},
            )
            return query

        if context.message and context.message.parts:
            logger.debug(
                f"Extracting user action from {len(context.message.parts)} message parts",
                extra={"agent_name": getattr(self.agent, "name", "k8sAutopilotAgent")},
            )
            action_query = self._extract_user_action(context.message.parts)
            if action_query:
                return action_query

        return query

    def _extract_user_action(self, parts: Sequence[Part]) -> str | None:
        """Parse A2UI ``userAction`` from message DataParts."""
        for part in parts:
            user_action = self._get_user_action_from_part(part)
            if user_action is None:
                continue

            query = self._resolve_action_to_query(user_action)
            logger.info(
                "Extracted client userAction payload",
                extra={
                    "action": query[:300],
                    "agent_name": getattr(self.agent, "name", "k8sAutopilotAgent"),
                },
            )
            return query

        return None

    @staticmethod
    def _get_user_action_from_part(part: Part) -> dict | None:
        """Extract ``userAction`` dict from a Part, or None."""
        if part.HasField("data"):
            from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

            data = MessageToDict(part.data)
            logger.debug("Decoded protobuf DataPart payload", extra={"raw_data": str(data)[:300]})
            if isinstance(data, dict):
                if "userAction" in data and isinstance(data["userAction"], dict):
                    return data["userAction"]
                return data
        return None

    @staticmethod
    def _resolve_action_to_query(user_action: Any) -> str:
        """Convert a ``userAction`` dict into a query string / payload."""
        if not isinstance(user_action, dict):
            return json.dumps(user_action)

        action_name = user_action.get("name") or user_action.get("actionId") or user_action.get("action") or ""

        # Handle ask_user_response
        if action_name == "ask_user_response" or "answers" in user_action:
            val = user_action.get("value")
            if isinstance(val, dict) and "answers" in val:
                return json.dumps(val)
            if "answers" in user_action:
                return json.dumps({
                    "status": user_action.get("status", "answered"),
                    "answers": user_action.get("answers", []),
                })
            answers_list: list[str] = []
            for item in user_action.get("context", []):
                if isinstance(item, dict) and item.get("key") == "answers":
                    raw = item.get("value")
                    if isinstance(raw, list):
                        answers_list = [str(x) for x in raw]
                    elif isinstance(raw, str):
                        answers_list = [raw]
            if answers_list:
                return json.dumps({"status": "answered", "answers": answers_list})
            return json.dumps(user_action)

        # Handle goal_response
        if action_name in ("goal_response", "goal_review_response"):
            val = user_action.get("value")
            if isinstance(val, dict):
                return json.dumps(val)
            return json.dumps(user_action)

        raw_ctx = user_action.get("context")
        has_decision = (
            "decision" in user_action
            or (isinstance(raw_ctx, dict) and "decision" in raw_ctx)
        )
        if action_name != "hitl_response" and not has_decision:
            val = user_action.get("value")
            if isinstance(val, dict):
                return json.dumps(val)
            return json.dumps(user_action)

        ctx: dict[str, Any] = {}
        if isinstance(raw_ctx, dict):
            ctx.update(raw_ctx)
        elif isinstance(raw_ctx, list):
            for item in raw_ctx:
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

        if "decision" in user_action and not ctx.get("decision"):
            ctx["decision"] = str(user_action["decision"])

        decision = ctx.get("decision", "").strip()

        form_inputs = user_action.get("formInputs", {})
        if form_inputs:
            for k, v in form_inputs.items():
                if isinstance(v, dict) and "stringInputs" in v:
                    strings = v["stringInputs"].get("value", [])
                    if strings:
                        ctx[k] = strings[0]
                elif isinstance(v, str):
                    ctx[k] = v

        non_decision_keys = {k for k in ctx if k != "decision" and ctx[k]}
        if decision:
            payload: dict[str, Any] = {"action": "hitl_response", "decision": decision}
            if non_decision_keys:
                payload.update({k: ctx[k] for k in non_decision_keys})
            return json.dumps(payload)

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
            # If current task is already terminal, start a new task for the new turn
            if task.status and task.status.state in (
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
            ):
                if context.message:
                    task = new_task_from_user_message(context.message)
                    await event_queue.enqueue_event(task)
                    return task
            return task

        if context.message is None:
            raise A2AError(message="No message provided in request context.")

        task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        return task

    # ── Step 3: Resume wrapping ───────────────────────────────────────

    @staticmethod
    async def _get_pending_interrupts(
        agent_graph: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Extract all pending interrupts from top-level state, tasks, and nested subgraphs."""
        pending_interrupts: dict[str, Any] = {}
        if agent_graph is None or not hasattr(agent_graph, "aget_state"):
            return pending_interrupts

        try:
            try:
                graph_state = await agent_graph.aget_state(config, subgraphs=True)
            except TypeError:
                graph_state = await agent_graph.aget_state(config)
            if graph_state:
                # Top-level interrupts
                for int_obj in getattr(graph_state, "interrupts", ()) or ():
                    int_id = getattr(int_obj, "id", None) or (
                        int_obj.get("id") if isinstance(int_obj, dict) else None
                    )
                    int_val = getattr(int_obj, "value", None) or (
                        int_obj.get("value") if isinstance(int_obj, dict) else None
                    )
                    if int_id and int_val:
                        pending_interrupts[int_id] = int_val

                # Nested tasks and subgraphs
                tasks_to_check = list(getattr(graph_state, "tasks", ()) or ())
                while tasks_to_check:
                    t = tasks_to_check.pop(0)
                    for int_obj in getattr(t, "interrupts", ()) or ():
                        int_id = getattr(int_obj, "id", None) or (
                            int_obj.get("id") if isinstance(int_obj, dict) else None
                        )
                        int_val = getattr(int_obj, "value", None) or (
                            int_obj.get("value") if isinstance(int_obj, dict) else None
                        )
                        if int_id and int_val:
                            pending_interrupts[int_id] = int_val
                    sub_state = getattr(t, "state", None)
                    if sub_state and getattr(sub_state, "tasks", None):
                        tasks_to_check.extend(sub_state.tasks)
        except Exception as exc:
            logger.debug(f"Graph state check for interrupts failed: {exc}")

        return pending_interrupts

    @staticmethod
    async def _persist_auto_mode(agent_graph: Any, config: dict[str, Any], task: Task) -> None:
        """Persist auto approval mode to thread store and runtime settings."""
        tid = (
            config.get("configurable", {}).get("thread_id")
            or getattr(task, "context_id", None)
            or getattr(task, "id", None)
        )
        if tid:
            try:
                from k8s_autopilot.security.approval_mode import awrite_approval_mode

                await awrite_approval_mode(agent_graph, str(tid), mode="auto")
            except Exception as exc:
                logger.debug(f"Failed to persist auto mode to LangGraph store: {exc}")

        try:
            from k8s_autopilot.api.settings_routes import get_config_store
            from k8s_autopilot.config.store import ConfigCategory

            cfg_store = await get_config_store()
            await cfg_store.set(
                key="APPROVAL_MODE",
                value="auto",
                category=ConfigCategory.SECURITY,
                display_name="Approval Mode",
            )
        except Exception as exc:
            logger.debug(f"Failed to persist auto mode to ConfigStore: {exc}")

        from k8s_autopilot.config.settings import get_settings
        get_settings().approval_mode = "auto"

    @staticmethod
    async def _wrap_resume(
        agent_graph: Any,
        config: dict[str, Any],
        task: Task,
        query: Any,
    ) -> str | Command[Any] | None:
        """Wrap ``query`` in ``Command(resume=...)`` matching OpsCode's interrupt resolution."""
        if isinstance(query, Command):
            return query

        from k8s_autopilot.schema.interrupts import (
            AskUserResumePayload,
            GoalReviewResumePayload,
            HitlResumePayload,
        )

        pending_interrupts = await A2AAutoPilotExecutor._get_pending_interrupts(agent_graph, config)

        # Parse user query if JSON encoded
        parsed_query: Any = query
        if isinstance(query, str) and query.strip():
            try:
                parsed_query = json.loads(query)
            except Exception:
                parsed_query = query

        logger.debug(
            "Wrap resume query check",
            extra={
                "task_id": task.id if task else "unknown",
                "pending_interrupt_ids": list(pending_interrupts.keys()),
                "parsed_query": str(parsed_query)[:300],
            },
        )

        if pending_interrupts:
            resume_payload: dict[str, Any] = {}
            for int_id, int_val in pending_interrupts.items():
                if not isinstance(int_val, dict):
                    resume_payload[int_id] = parsed_query
                    continue

                # 1. ask_user interrupt
                if int_val.get("type") == "ask_user" or "questions" in int_val:
                    questions = int_val.get("questions", [])
                    payload = AskUserResumePayload.from_raw(parsed_query, questions=questions)
                    resume_payload[int_id] = payload.model_dump()
                    continue

                # 2. HITL tool authorization interrupt
                if "action_requests" in int_val or int_val.get("type") == "hitl":
                    req_count = len(int_val.get("action_requests", [])) or 1
                    hitl_payload = HitlResumePayload.from_raw(parsed_query, count=req_count)
                    if hitl_payload.auto_approve_requested:
                        await A2AAutoPilotExecutor._persist_auto_mode(agent_graph, config, task)
                    resume_payload[int_id] = {
                        "decisions": [d.model_dump(exclude_none=True) for d in hitl_payload.decisions]
                    }
                    continue

                # 3. Goal review / confirmation interrupt
                if (
                    int_val.get("type") in ("goal_review", "goal_confirmation", "goal_criteria")
                    or ("objective" in int_val and "criteria" in int_val)
                    or "goal" in int_val
                ):
                    goal_payload = GoalReviewResumePayload.from_raw(parsed_query)
                    resume_payload[int_id] = goal_payload.model_dump(exclude_none=True)
                    continue

                # 4. Default fallback for other interrupts
                resume_payload[int_id] = parsed_query

            logger.info(
                "Resuming graph from interrupt — wrapping as Command(resume=...)",
                extra={
                    "task_id": task.id if task else "unknown",
                    "resume_payload": resume_payload,
                    "preview": str(resume_payload)[:200],
                },
            )
            return Command(resume=resume_payload)

        # Fallback when no pending_interrupts found from checkpointer
        is_interrupted = False
        if task and hasattr(task, "status") and hasattr(task.status, "state"):
            if task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                is_interrupted = True

        if isinstance(parsed_query, dict) and (
            "status" in parsed_query
            or "answers" in parsed_query
            or "decision" in parsed_query
            or "decisions" in parsed_query
            or "action" in parsed_query
        ):
            is_interrupted = True

        # Check if parsed_query is string but represents an approval choice or action
        if isinstance(parsed_query, str):
            choice = parsed_query.lower().strip()
            if choice in ("approve", "yes", "confirm", "auto", "auto_approve_all", "reject", "deny", "cancel"):
                is_interrupted = True
                if choice in ("auto", "auto_approve_all"):
                    await A2AAutoPilotExecutor._persist_auto_mode(agent_graph, config, task)
                    parsed_query = "approve"

        if is_interrupted:
            if isinstance(parsed_query, dict) and "decision" in parsed_query:
                dec_str = str(parsed_query["decision"]).lower().strip()
                if dec_str in ("auto_approve_all", "enable_auto", "auto", "a"):
                    await A2AAutoPilotExecutor._persist_auto_mode(agent_graph, config, task)
            logger.info(
                "Resuming graph from interrupt (fallback) — wrapping as Command(resume=...)",
                extra={
                    "task_id": task.id if task else "unknown",
                    "preview": str(parsed_query)[:100],
                },
            )
            return Command(resume=parsed_query)

        if isinstance(query, (str, type(None))) or isinstance(query, Command):
            return query
        return str(query)



    # ── Step 4: Agent streaming (Pregel LangGraph Stream) ──────────────

    def _prepare_stream_input(self, query: str | Command[Any] | None) -> Any:
        """Prepare the graph input dict or Command from user query."""
        if isinstance(query, Command):
            return query

        from k8s_autopilot.middleware.auto_mode import (
            USER_PROMPT_METADATA_KEY,
            user_prompt_metadata,
        )

        turn_id = str(uuid.uuid4())
        prompt_text = str(query or "")
        prompt_meta = user_prompt_metadata(
            literal_user_text=prompt_text,
            referenced_paths=[],
            turn_id=turn_id,
        )
        human_msg = HumanMessage(
            content=prompt_text,
            additional_kwargs={
                USER_PROMPT_METADATA_KEY: prompt_meta,
            },
        )
        return {
            "messages": [human_msg],
            "goal_criteria_request": None,
        }

    def _prepare_stream_agent_graph(
        self,
        requested_model: str | None,
        requested_effort: str | None,
        agent_graph: Any,
        context_id: str,
        task_id: str,
        config: dict[str, Any] | None,
    ) -> tuple[Any, dict[str, Any], str, str]:
        """Resolve model, effort, agent graph, and graph execution config."""
        from k8s_autopilot.config.settings import get_settings
        from k8s_autopilot.model.reasoning import with_effort_model_params

        settings = get_settings()
        if agent_graph is None:
            active_model: str = requested_model or settings.model or settings.model_name or "gemini-3.7-flash"
            active_effort: str = requested_effort or getattr(settings, "reasoning_effort", None) or "medium"
            agent_graph = self._ensure_agent(
                requested_model=active_model,
                requested_effort=active_effort,
            )
        else:
            active_model = requested_model or "gemini-3.7-flash"
            active_effort = requested_effort or "medium"

        if config is None:
            config = {}
        if "configurable" not in config or not isinstance(config["configurable"], dict):
            config["configurable"] = {}
        if "metadata" not in config or not isinstance(config["metadata"], dict):
            config["metadata"] = {}

        config["configurable"]["thread_id"] = context_id
        config["run_name"] = f"k8s-autopilot:{task_id[:8]}"
        config["tags"] = ["k8s-autopilot", f"thread:{context_id}"]
        config["metadata"].update({
            "thread_id": context_id,
            "task_id": task_id,
            "session_id": context_id,
        })
        return agent_graph, config, active_model, active_effort

    async def _init_stream_telemetry(
        self,
        agent_graph: Any,
        context_id: str,
        active_model: str,
        active_effort: str,
        requested_mode: str | None = None,
    ) -> _StreamTelemetryState:
        """Initialize telemetry state and populate initial approval mode from store.

        Resolution chain (highest priority last):
          1. Settings singleton (startup default)
          2. LangGraph thread-scoped in-memory store (from previous writes in same session)
          3. Inbound requested_mode from A2A message metadata (highest priority)

        NOTE: ConfigStore DB read removed — mode is session-scoped and flows
        via A2A metadata + LangGraph in-memory store only.
        """
        from k8s_autopilot.config.settings import get_settings
        from k8s_autopilot.security.approval_mode import (
            approval_mode_key,
            aread_approval_mode_from_store,
            awrite_approval_mode,
        )

        # 1. Settings singleton (startup default)
        approval_mode: str = getattr(get_settings(), "approval_mode", "manual") or "manual"

        # 2. Thread-scoped LangGraph in-memory store
        try:
            store = getattr(agent_graph, "store", None)
            if store:
                stored_mode = await aread_approval_mode_from_store(
                    store, approval_mode_key(context_id)
                )
                if stored_mode:
                    approval_mode = stored_mode.value
        except Exception:
            pass

        # 3. Inbound request mode (highest priority when explicitly provided)
        if requested_mode and requested_mode.strip().lower() in ("manual", "auto", "yolo"):
            approval_mode = requested_mode.strip().lower()

        # 4. Immediately sync active mode to the LangGraph store for this thread
        live_key: str | None = None
        try:
            live_key = await awrite_approval_mode(agent_graph, context_id, mode=approval_mode)
        except Exception as exc:
            logger.debug(f"Failed to persist live approval mode to store in _init_stream_telemetry: {exc}")

        # 5. Load historical thread baseline tokens and cost from SessionManager
        init_in = 0
        init_out = 0
        init_cost = 0.0
        seen_msg_tokens: dict[str, tuple[int, int]] = {}
        try:
            from k8s_autopilot.state.session import SessionManager

            sm_in, sm_out, sm_cost, msg_ids = await SessionManager().get_thread_token_usage_and_cost(context_id)
            init_in = sm_in
            init_out = sm_out
            init_cost = sm_cost
            for mid in msg_ids:
                seen_msg_tokens[mid] = (-1, -1)
        except Exception as exc:
            logger.debug(f"Failed loading thread telemetry baseline in _init_stream_telemetry: {exc}")

        # 6. Restore authoritative goal and rubric state from checkpoint snapshot
        init_goal_status: str | None = None
        init_goal_obj: str | None = None
        init_rubric: str | None = None
        if agent_graph is not None and hasattr(agent_graph, "aget_state"):
            try:
                config = {"configurable": {"thread_id": context_id}}
                try:
                    state_snapshot = await agent_graph.aget_state(config, subgraphs=True)
                except TypeError:
                    state_snapshot = await agent_graph.aget_state(config)
                if state_snapshot and state_snapshot.values:
                    vals = state_snapshot.values
                    if vals.get("_session_cost_usd") is not None:
                        try:
                            val = float(vals["_session_cost_usd"])
                            if math.isfinite(val) and val >= 0:
                                init_cost = val
                        except Exception:
                            pass
                    if "_goal_status" in vals and vals["_goal_status"]:
                        init_goal_status = str(vals["_goal_status"])
                    if "_rubric_status" in vals and vals["_rubric_status"]:
                        r_stat = str(vals["_rubric_status"]).lower()
                        if r_stat in ("satisfied", "passed", "complete"):
                            init_goal_status = "complete"
                        elif r_stat in ("max_iterations_reached", "failed", "blocked") and init_goal_status != "complete":
                            init_goal_status = "blocked"
                    if "_goal_objective" in vals and vals["_goal_objective"]:
                        init_goal_obj = str(vals["_goal_objective"])
                    raw_rubric = (
                        vals.get("rubric")
                        or vals.get("_goal_rubric")
                        or vals.get("_sticky_rubric")
                        or vals.get("criteria")
                    )
                    if isinstance(raw_rubric, list):
                        init_rubric = "\n".join(f"- {c}" for c in raw_rubric if c)
                    elif isinstance(raw_rubric, str) and raw_rubric.strip():
                        init_rubric = raw_rubric.strip()
            except Exception as exc:
                logger.debug(f"Failed loading thread goal state in _init_stream_telemetry: {exc}")

        # Fallback to direct DB read via SessionManager if snapshot didn't have goal
        if not init_goal_obj or not init_rubric:
            try:
                from k8s_autopilot.state.session import SessionManager

                db_goal = await SessionManager().get_thread_goal_state(context_id)
                if not init_goal_obj and db_goal.get("objective"):
                    init_goal_obj = db_goal["objective"]
                if not init_goal_status and db_goal.get("status"):
                    init_goal_status = db_goal["status"]
                if not init_rubric and db_goal.get("rubric"):
                    init_rubric = db_goal["rubric"]
            except Exception as exc:
                logger.debug(f"Failed fallback DB read for goal state in _init_stream_telemetry: {exc}")

        if init_rubric and not init_goal_status:
            init_goal_status = "active"

        return _StreamTelemetryState(
            cumulative_input_tokens=init_in,
            cumulative_output_tokens=init_out,
            cumulative_cost_usd=init_cost,
            processed_message_tokens=seen_msg_tokens,
            current_approval_mode=approval_mode,
            approval_mode_key=live_key,
            active_model=active_model,
            active_effort=active_effort,
            current_goal_status=init_goal_status,
            current_goal_objective=init_goal_obj,
            current_rubric=init_rubric,
        )

    def _attach_stream_trace(self, msg_obj: Message, event_type: str, ctx: _StreamContext) -> None:
        """Attach trace metadata to an outgoing message using current stream telemetry state."""
        t = ctx.telemetry
        self._attach_trace_metadata(
            msg_obj,
            t.run_id,
            t.step_index,
            event_type,
            approval_mode=t.current_approval_mode,
            goal_status=t.current_goal_status,
            goal_objective=t.current_goal_objective,
            goal_rubric=t.current_rubric,
            rubric_active=bool(t.current_rubric),
            rubric_label=t.derive_rubric_label(),
            input_tokens=t.cumulative_input_tokens,
            output_tokens=t.cumulative_output_tokens,
            cost_usd=t.cumulative_cost_usd,
            model=t.active_model,
            reasoning_effort=t.active_effort,
        )

    async def _flush_active_tool_surfaces(
        self, ctx: _StreamContext, status: str = "error"
    ) -> None:
        """Close any outstanding running tool surfaces with given status."""
        for _t_key, tracked in list(ctx.active_tool_surfaces.items()):
            duration = int((time.monotonic() - tracked["start_time"]) * 1000)
            term_out = (
                "Tool execution paused."
                if status == "paused"
                else "Tool execution completed."
                if status == "success"
                else "Tool execution finished."
            )
            update_op = update_tool_execution_data(
                tracked["surface_id"],
                toolName=tracked["tool_name"],
                parameters=tracked["parameters"],
                environment=tracked["environment"],
                status=status,
                terminalOutput=term_out,
                durationMs=duration,
            )
            parts = [create_a2ui_part(update_op)]
            msg = self._make_stream_message_parts(
                parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
            )
            self._attach_stream_trace(msg, "tool_result", ctx)
            await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg)
            ctx.telemetry.step_index += 1
        ctx.active_tool_surfaces.clear()

    def _process_message_telemetry(self, msg: Any, ctx: _StreamContext) -> None:
        """Accumulate token counts and estimate cost for a message if not already processed."""
        if not msg:
            return
        t = ctx.telemetry
        mid = getattr(msg, "id", None)
        if mid and mid in t.processed_message_tokens and t.processed_message_tokens[mid] == (-1, -1):
            return

        usage = (
            getattr(msg, "usage_metadata", None)
            or getattr(msg, "response_metadata", {}).get("token_usage")
            or getattr(msg, "response_metadata", {}).get("usage")
        )
        if not isinstance(usage, dict):
            return

        new_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        new_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        if not new_in and not new_out:
            return

        prev_in, prev_out = t.processed_message_tokens.get(mid, (0, 0)) if mid else (0, 0)
        delta_in = max(0, new_in - prev_in)
        delta_out = max(0, new_out - prev_out)

        if delta_in > 0 or delta_out > 0:
            t.cumulative_input_tokens += delta_in
            t.cumulative_output_tokens += delta_out
            if mid:
                t.processed_message_tokens[mid] = (max(prev_in, new_in), max(prev_out, new_out))

    async def _handle_messages_chunk(self, payload: Any, ctx: _StreamContext) -> None:
        """Handle a chunk from mode == 'messages'."""
        if isinstance(payload, tuple) and len(payload) >= 1:
            msg = payload[0]
            meta = payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else {}
        else:
            msg = payload
            meta = {}

        if is_conversation_control_message(msg):
            return

        self._process_message_telemetry(msg, ctx)

        if isinstance(msg, (AIMessage, AIMessageChunk)):
            await self._handle_aimessage_chunk(msg, meta, ctx)
        elif isinstance(msg, ToolMessage):
            await self._handle_tool_message(msg, ctx)

    async def _handle_aimessage_chunk(
        self, msg: AIMessage | AIMessageChunk, meta: dict[str, Any], ctx: _StreamContext
    ) -> None:
        """Process AIMessage / AIMessageChunk text, thinking tokens, and tool calls."""
        text, thinking = _extract_text_and_thinking(
            msg.content,
            additional_kwargs=getattr(msg, "additional_kwargs", None),
            response_metadata=getattr(msg, "response_metadata", None),
            msg_obj=msg,
        )

        if thinking:
            if ctx.use_ui:
                ctx.reasoning_buffer += thinking
                agent_label = ctx.renderer._resolve_agent(meta) or "Reasoning"
                if not ctx.reasoning_surface_id:
                    ctx.reasoning_surface_id = f"thought-{uuid.uuid4().hex[:8]}"
                    ops = build_thought_block_surface(
                        surface_id=ctx.reasoning_surface_id,
                        title=agent_label,
                        summary=ctx.reasoning_buffer,
                        severity="info",
                    )
                    for op in ops:
                        parts = [create_a2ui_part(op)]
                        msg_out = self._make_stream_message_parts(
                            parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                        )
                        self._attach_stream_trace(msg_out, "reasoning", ctx)
                        await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                        ctx.telemetry.step_index += 1
                        await asyncio.sleep(0)
                else:
                    update_op = update_thought_block_data(
                        ctx.reasoning_surface_id,
                        summary=ctx.reasoning_buffer,
                    )
                    parts = [create_a2ui_part(update_op)]
                    msg_out = self._make_stream_message_parts(
                        parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                    )
                    self._attach_stream_trace(msg_out, "reasoning", ctx)
                    await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                    ctx.telemetry.step_index += 1
                    await asyncio.sleep(0)
            else:
                await ctx.renderer.open_thinking()
                await ctx.renderer.emit_with_label(thinking, meta)

        if text:
            if ctx.reasoning_surface_id:
                ctx.reasoning_surface_id = None
                ctx.reasoning_buffer = ""
            await ctx.renderer.close_thinking()
            await ctx.renderer.emit_with_label(text, meta)

        tool_calls = (
            getattr(msg, "tool_calls", None)
            or getattr(msg, "tool_call_chunks", None)
        )
        if tool_calls:
            await self._handle_tool_call_chunks(tool_calls, meta, ctx)

    async def _handle_tool_call_chunks(
        self, tool_call_chunks: Sequence[Any], meta: dict[str, Any], ctx: _StreamContext
    ) -> None:
        """Render tool call starts and planning cards."""
        for tc in tool_call_chunks:
            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            tc_id = (tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)) or str(uuid.uuid4())
            raw_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_args: dict[str, Any] = {}
            if isinstance(raw_args, dict):
                tc_args = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    parsed_args = json.loads(raw_args)
                    if isinstance(parsed_args, dict):
                        tc_args = parsed_args
                except Exception:
                    tc_args = {}

            if not tc_name:
                continue

            if ctx.use_ui:
                if ctx.reasoning_surface_id:
                    ctx.reasoning_surface_id = None
                    ctx.reasoning_buffer = ""
                await ctx.renderer.close_thinking()

                if tc_name == "write_todos":
                    todos_list = []
                    if isinstance(tc_args, dict):
                        todos_list = tc_args.get("todos", [])
                    elif isinstance(tc_args, list):
                        todos_list = tc_args
                    if todos_list:
                        if not ctx.plan_todo_surface_id:
                            ctx.plan_todo_surface_id = f"plan-todo-{uuid.uuid4().hex[:8]}"
                            ops = build_plan_todo_surface(
                                surface_id=ctx.plan_todo_surface_id,
                                todos=todos_list,
                                plan_title="Execution Plan",
                                plan_version=1,
                            )
                            for op in ops:
                                parts = [create_a2ui_part(op)]
                                msg_out = self._make_stream_message_parts(
                                    parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                                )
                                self._attach_stream_trace(msg_out, "plan_todo", ctx)
                                await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                                ctx.telemetry.step_index += 1
                                await asyncio.sleep(0)
                        else:
                            update_op = update_plan_todo_data(
                                ctx.plan_todo_surface_id,
                                todos=todos_list,
                            )
                            parts = [create_a2ui_part(update_op)]
                            msg_out = self._make_stream_message_parts(
                                parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                            )
                            self._attach_stream_trace(msg_out, "plan_todo_update", ctx)
                            await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                            ctx.telemetry.step_index += 1
                            await asyncio.sleep(0)
                elif tc_name in ("propose_goal", "get_goal", "get_rubric", "update_goal"):
                    # Goal lifecycle tools have their own specialized interrupt surfaces
                    # or are internal queries. Do not emit generic terminal execution cards.
                    pass
                else:
                    display_name = _humanize_tool_name(tc_name)
                    surface_id = f"tool-{tc_id}" if tc_id else f"tool-{tc_name}-{uuid.uuid4().hex[:8]}"
                    ops = build_tool_execution_surface(
                        surface_id=surface_id,
                        tool_name=display_name,
                        status="running",
                        parameters=tc_args,
                        environment=meta.get("environment", ""),
                    )
                    for op in ops:
                        parts = [create_a2ui_part(op)]
                        msg_out = self._make_stream_message_parts(
                            parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                        )
                        self._attach_stream_trace(msg_out, "tool_call", ctx)
                        await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                        ctx.telemetry.step_index += 1
                        await asyncio.sleep(0)

                    ctx.active_tool_surfaces[tc_id or tc_name] = {
                        "surface_id": surface_id,
                        "start_time": time.monotonic(),
                        "tool_name": display_name,
                        "parameters": tc_args,
                        "environment": meta.get("environment", ""),
                    }

    async def _handle_tool_message(self, msg: ToolMessage, ctx: _StreamContext) -> None:
        """Process ToolMessage result completion and embedded A2UI surfaces."""
        tool_call_id = getattr(msg, "tool_call_id", None) or ""
        tool_name = getattr(msg, "name", "tool") or "tool"
        content_out = str(getattr(msg, "content", ""))
        status_str = getattr(msg, "status", "success")
        is_error = status_str == "error" or (isinstance(content_out, str) and content_out.startswith("Error:"))

        if tool_name == "propose_goal":
            if content_out and not is_error:
                # Update status bar telemetry for goal confirmation without emitting an intermediate agent chat message.
                meta_msg = self._make_stream_message_parts(
                    [],
                    ctx.renderer.message_id,
                    ctx.context_id,
                    ctx.task.id,
                )
                self._attach_stream_trace(meta_msg, "goal_confirmed", ctx)
                await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, meta_msg)
                ctx.telemetry.step_index += 1
                await asyncio.sleep(0)
            return

        if ctx.use_ui:
            tracked = ctx.active_tool_surfaces.pop(tool_call_id, None) or ctx.active_tool_surfaces.pop(tool_name, None)
            if not tracked:
                for k, v in list(ctx.active_tool_surfaces.items()):
                    if v.get("tool_name") == tool_name or tool_name in ("task", "subagent", "js_eval"):
                        tracked = ctx.active_tool_surfaces.pop(k)
                        break

            surface_id = tracked["surface_id"] if tracked else (f"tool-{tool_call_id}" if tool_call_id else f"tool-{tool_name}")
            disp_name = tracked["tool_name"] if tracked else _humanize_tool_name(tool_name)
            params = tracked["parameters"] if tracked else {}
            env = tracked["environment"] if tracked else ""
            duration = int((time.monotonic() - tracked["start_time"]) * 1000) if tracked else 0

            update_op = update_tool_execution_data(
                surface_id,
                toolName=disp_name,
                parameters=params,
                environment=env,
                status="error" if is_error else "success",
                terminalOutput=content_out[:4000],
                durationMs=duration,
            )
            parts = [create_a2ui_part(update_op)]
            msg_out = self._make_stream_message_parts(
                parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
            )
            self._attach_stream_trace(msg_out, "tool_result", ctx)
            await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
            ctx.telemetry.step_index += 1
            await asyncio.sleep(0)

            if tool_name in ("build_obs_a2ui", "build_obs_dashboard", "generate_a2ui", "generate_obs_a2ui") or "a2ui_operations" in content_out:
                try:
                    parsed_res = json.loads(content_out)
                    if isinstance(parsed_res, dict) and "a2ui_operations" in parsed_res:
                        a2ui_parts = [create_a2ui_part(op) for op in parsed_res["a2ui_operations"]]
                        if a2ui_parts:
                            msg_out = self._make_stream_message_parts(
                                a2ui_parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                            )
                            self._attach_stream_trace(msg_out, "tool_result_a2ui", ctx)
                            await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                            ctx.telemetry.step_index += 1
                            await asyncio.sleep(0)
                except Exception:
                    pass

    def _resolve_subagent_name(
        self, namespace: tuple[str, ...], meta: dict[str, Any], ctx: _StreamContext
    ) -> str:
        """Resolve a human-friendly subagent name from namespace, meta, or active tasks."""
        if meta:
            for key in ("subagent_type", "subagent", "agent_name", "ls_agent_type"):
                val = meta.get(key)
                if val and isinstance(val, str) and val not in ("subagent", "agent"):
                    return _humanize_tool_name(val)

        for seg in namespace:
            for tc_id, tracked in ctx.active_tool_surfaces.items():
                if tc_id and tc_id in seg:
                    params = tracked.get("parameters") or {}
                    sub_type = params.get("subagent_type") or params.get("name") or params.get("agent")
                    if sub_type:
                        return _humanize_tool_name(str(sub_type))

        for seg in namespace:
            for part in seg.split(":"):
                if "operator" in part.lower() or "agent" in part.lower():
                    return _humanize_tool_name(part)

        return "Subagent"

    async def _handle_subagent_messages_chunk(
        self, payload: Any, namespace: tuple[str, ...], ctx: _StreamContext
    ) -> None:
        """Handle stream chunks from child subagent graphs without polluting main chat text."""
        if isinstance(payload, tuple) and len(payload) >= 1:
            msg = payload[0]
            meta = payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else {}
        else:
            msg = payload
            meta = {}

        if is_conversation_control_message(msg):
            return

        subagent_name = self._resolve_subagent_name(namespace, meta, ctx)

        # Track token usage and step cost from subagent message if available
        self._process_message_telemetry(msg, ctx)

        if isinstance(msg, (AIMessage, AIMessageChunk)):
            text, thinking = _extract_text_and_thinking(
                msg.content,
                additional_kwargs=getattr(msg, "additional_kwargs", None),
                response_metadata=getattr(msg, "response_metadata", None),
                msg_obj=msg,
            )

            if thinking and ctx.use_ui:
                ns_key = ":".join(x for x in namespace)
                ctx.subagent_reasoning_buffers[ns_key] = (
                    ctx.subagent_reasoning_buffers.get(ns_key, "") + thinking
                )
                surface_id = ctx.subagent_reasoning_surfaces.get(ns_key)
                if not surface_id:
                    surface_id = f"thought-subagent-{uuid.uuid4().hex[:8]}"
                    ctx.subagent_reasoning_surfaces[ns_key] = surface_id
                    ops = build_thought_block_surface(
                        surface_id=surface_id,
                        title=f"{subagent_name} (Reasoning)",
                        summary=ctx.subagent_reasoning_buffers[ns_key],
                        severity="info",
                    )
                    for op in ops:
                        parts = [create_a2ui_part(op)]
                        msg_out = self._make_stream_message_parts(
                            parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                        )
                        self._attach_stream_trace(msg_out, "subagent_reasoning", ctx)
                        await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                        ctx.telemetry.step_index += 1
                        await asyncio.sleep(0)
                else:
                    update_op = update_thought_block_data(
                        surface_id,
                        summary=ctx.subagent_reasoning_buffers[ns_key],
                    )
                    parts = [create_a2ui_part(update_op)]
                    msg_out = self._make_stream_message_parts(
                        parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                    )
                    self._attach_stream_trace(msg_out, "subagent_reasoning", ctx)
                    await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                    ctx.telemetry.step_index += 1
                    await asyncio.sleep(0)

            tool_calls = (
                getattr(msg, "tool_calls", None)
                or getattr(msg, "tool_call_chunks", None)
            )
            if tool_calls:
                sub_meta = dict(meta)
                sub_meta["environment"] = subagent_name
                await self._handle_tool_call_chunks(tool_calls, sub_meta, ctx)

        elif isinstance(msg, ToolMessage):
            await self._handle_tool_message(msg, ctx)

    async def _handle_updates_chunk(self, payload: Any, ctx: _StreamContext) -> bool:
        """Handle mode == 'updates', returning True if execution interrupted."""
        if not isinstance(payload, dict):
            return False

        t = ctx.telemetry
        for node_name, node_val in payload.items():
            if isinstance(node_val, dict):
                if "_goal_status" in node_val:
                    t.current_goal_status = str(node_val["_goal_status"])
                if "_rubric_status" in node_val:
                    rubric_stat = str(node_val["_rubric_status"]).lower()
                    if rubric_stat in ("satisfied", "passed", "complete"):
                        t.current_goal_status = "complete"
                    elif rubric_stat in ("max_iterations_reached", "failed", "blocked"):
                        t.current_goal_status = "blocked"
                if "_goal_objective" in node_val:
                    t.current_goal_objective = str(node_val["_goal_objective"])
                if "rubric" in node_val and node_val["rubric"]:
                    t.current_rubric = node_val["rubric"]
                elif "_goal_rubric" in node_val and node_val["_goal_rubric"]:
                    t.current_rubric = node_val["_goal_rubric"]
                elif "_sticky_rubric" in node_val and node_val["_sticky_rubric"]:
                    t.current_rubric = node_val["_sticky_rubric"]
                elif "criteria" in node_val and node_val["criteria"]:
                    t.current_rubric = node_val["criteria"]

                if isinstance(t.current_rubric, list):
                    t.current_rubric = "\n".join(f"- {c}" for c in t.current_rubric if c)
                elif isinstance(t.current_rubric, str):
                    t.current_rubric = t.current_rubric.strip()

                if t.current_rubric and not t.current_goal_status:
                    t.current_goal_status = "active"

                if "_session_cost_usd" in node_val and node_val["_session_cost_usd"] is not None:
                    try:
                        val = float(node_val["_session_cost_usd"])
                        if math.isfinite(val) and val >= 0:
                            t.cumulative_cost_usd = val
                    except Exception:
                        pass
                if "approval_mode" in node_val:
                    t.current_approval_mode = str(node_val["approval_mode"])

                node_msgs = node_val.get("messages")
                if node_msgs:
                    node_msg_list = node_msgs if isinstance(node_msgs, list) else [node_msgs]
                    for m in node_msg_list:
                        self._process_message_telemetry(m, ctx)

        interrupt_val = payload.get("__interrupt__")
        if interrupt_val:
            if not ctx.interrupted:
                ctx.interrupted = True
                if ctx.task and hasattr(ctx.task, "status") and hasattr(ctx.task.status, "state"):
                    ctx.task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
                await ctx.renderer.close_thinking()
                ctx.reasoning_surface_id = None
                ctx.reasoning_buffer = ""
                await self._flush_active_tool_surfaces(ctx, status="paused")
                await self._handle_interrupt_event(
                    interrupt_val=interrupt_val,
                    use_ui=ctx.use_ui,
                    renderer=ctx.renderer,
                    updater=ctx.updater,
                    context_id=ctx.context_id,
                    task_id=ctx.task.id,
                    run_id=t.run_id,
                    step_index=t.step_index,
                    approval_mode=t.current_approval_mode,
                    goal_status=t.current_goal_status,
                    goal_objective=t.current_goal_objective,
                    goal_rubric=t.current_rubric,
                    rubric_active=bool(t.current_rubric),
                    rubric_label=t.derive_rubric_label(),
                    input_tokens=t.cumulative_input_tokens,
                    output_tokens=t.cumulative_output_tokens,
                    cost_usd=t.cumulative_cost_usd,
                    model=t.active_model,
                    reasoning_effort=t.active_effort,
                )
            return True
        return False

    async def _handle_custom_chunk(self, payload: Any, ctx: _StreamContext) -> None:
        """Handle mode == 'custom' stream events."""
        if isinstance(payload, StreamResponse):
            payload_field = payload.WhichOneof("payload")
            if payload_field in ("status_update", "artifact_update"):
                await ctx.event_queue.enqueue_event(getattr(payload, payload_field))
        elif isinstance(payload, dict):
            if payload.get("type") == "session_cost" or payload.get("event") == "session_cost":
                evt_tid = payload.get("thread_id")
                if not evt_tid or str(evt_tid) == ctx.context_id:
                    cost_val = payload.get("total") or payload.get("cost_usd") or payload.get("total_cost_usd")
                    if cost_val is not None and not isinstance(cost_val, bool):
                        try:
                            val = float(cost_val)
                            if math.isfinite(val) and val >= 0:
                                ctx.telemetry.cumulative_cost_usd = val
                        except Exception:
                            pass
            if "goal_status" in payload:
                ctx.telemetry.current_goal_status = payload["goal_status"]
            if payload.get("type") == "subagent":
                phase = str(payload.get("phase") or "start")
                subagent_type = str(payload.get("subagent_type") or "subagent")
                desc = str(payload.get("description") or "")
                call_id = str(payload.get("id") or uuid.uuid4().hex[:8])
                surface_id = (
                    ctx.active_tool_surfaces[call_id]["surface_id"]
                    if call_id in ctx.active_tool_surfaces
                    else f"tool-task-{call_id}"
                )

                lifecycle_data = {
                    "type": "subagent_lifecycle",
                    "phase": phase,
                    "id": call_id,
                    "eval_id": payload.get("eval_id"),
                    "subagent_type": subagent_type,
                    "description": desc,
                    "duration_ms": payload.get("duration_ms"),
                }

                parts = []
                if ctx.use_ui:
                    if phase == "start":
                        if call_id not in ctx.active_tool_surfaces:
                            ops = build_tool_execution_surface(
                                surface_id=surface_id,
                                tool_name=f"Subagent: {subagent_type}",
                                status="running",
                                parameters={"task": desc, "subagent": subagent_type} if desc else {"subagent": subagent_type},
                            )
                            parts.extend(create_a2ui_part(op) for op in ops)
                            ctx.active_tool_surfaces[call_id] = {
                                "surface_id": surface_id,
                                "start_time": time.monotonic(),
                                "tool_name": f"Subagent: {subagent_type}",
                                "parameters": {"task": desc, "subagent": subagent_type},
                                "environment": "",
                            }
                        else:
                            update_op = update_tool_execution_data(
                                surface_id=surface_id,
                                status="running",
                                toolName=f"Subagent: {subagent_type}",
                            )
                            parts.append(create_a2ui_part(update_op))
                    elif phase in ("complete", "error"):
                        status_str = "success" if phase == "complete" else "error"
                        dur_ms = int(payload.get("duration_ms") or 0)
                        update_op = update_tool_execution_data(
                            surface_id=surface_id,
                            status=status_str,
                            durationMs=dur_ms,
                        )
                        parts.append(create_a2ui_part(update_op))
                        ctx.active_tool_surfaces.pop(call_id, None)

                msg_out = self._make_stream_message_parts(
                    parts, ctx.renderer.message_id, ctx.context_id, ctx.task.id
                )
                msg_out.metadata["subagent"] = json.dumps(payload)
                msg_out.metadata["subagent_lifecycle"] = json.dumps(lifecycle_data)
                self._attach_stream_trace(msg_out, "subagent_event", ctx)
                await ctx.updater.update_status(TaskState.TASK_STATE_WORKING, msg_out)
                ctx.telemetry.step_index += 1
                await asyncio.sleep(0)

    async def _finalize_stream_completion(
        self, ctx: _StreamContext, agent_graph: Any, config: dict[str, Any]
    ) -> None:
        """Finalize the turn upon successful completion."""
        await ctx.renderer.close_thinking()
        ctx.reasoning_surface_id = None
        ctx.reasoning_buffer = ""
        await self._flush_active_tool_surfaces(ctx, status="success")

        t = ctx.telemetry
        if agent_graph is not None:
            try:
                latest_state = await agent_graph.aget_state(config)
                if latest_state and latest_state.values:
                    vals = latest_state.values
                    if vals.get("_session_cost_usd") is not None:
                        try:
                            val = float(vals["_session_cost_usd"])
                            if math.isfinite(val) and val >= 0:
                                t.cumulative_cost_usd = val
                        except Exception:
                            pass
                    if "_goal_status" in vals and vals["_goal_status"]:
                        t.current_goal_status = str(vals["_goal_status"])
                    if "_rubric_status" in vals and vals["_rubric_status"]:
                        r_stat = str(vals["_rubric_status"]).lower()
                        if r_stat in ("satisfied", "passed", "complete"):
                            t.current_goal_status = "complete"
                        elif r_stat in ("max_iterations_reached", "failed", "blocked") and t.current_goal_status != "complete":
                            t.current_goal_status = "blocked"
                    if "_goal_objective" in vals and vals["_goal_objective"]:
                        t.current_goal_objective = str(vals["_goal_objective"])
                    raw_rubric = (
                        vals.get("rubric")
                        or vals.get("_goal_rubric")
                        or vals.get("_sticky_rubric")
                        or vals.get("criteria")
                    )
                    if isinstance(raw_rubric, list):
                        t.current_rubric = "\n".join(f"- {c}" for c in raw_rubric if c)
                    elif isinstance(raw_rubric, str) and raw_rubric.strip():
                        t.current_rubric = raw_rubric.strip()
                    if t.current_rubric and not t.current_goal_status:
                        t.current_goal_status = "active"

                    msgs = vals.get("messages", [])
                    for m in msgs:
                        self._process_message_telemetry(m, ctx)
            except Exception:
                pass

        accumulated = ctx.renderer.get_accumulated_response()
        final_text = "" if accumulated else "Task completed successfully."
        msg_out = self._make_stream_message_text(
            final_text,
            ctx.renderer.message_id,
            ctx.context_id,
            ctx.task.id,
        )
        self._attach_stream_trace(msg_out, "completed", ctx)
        await ctx.updater.update_status(TaskState.TASK_STATE_COMPLETED, msg_out)
        await self._safe_complete(ctx.updater, ctx.task)

    async def _stream_agent(
        self,
        query: str | Command[Any] | None,
        task: Task,
        updater: TaskUpdater,
        event_queue: EventQueue,
        context_id: str,
        use_ui: bool,
        requested_model: str | None = None,
        requested_effort: str | None = None,
        requested_mode: str | None = None,
        agent_graph: Any = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Stream agent Pregel graph events and dispatch A2A / A2UI updates."""
        agent_name = getattr(self.agent, "name", "k8sAutopilotAgent")
        logger.info(
            "Starting agent execution stream",
            task_id=task.id,
            context_id=context_id,
            extra={
                "agent_name": agent_name,
                "model": requested_model,
                "reasoning_effort": requested_effort,
                "is_resume": isinstance(query, Command),
            },
        )

        agent_graph, config, active_model, active_effort = self._prepare_stream_agent_graph(
            requested_model, requested_effort, agent_graph, context_id, task.id, config
        )
        telemetry = await self._init_stream_telemetry(
            agent_graph, context_id, active_model, active_effort, requested_mode=requested_mode
        )
        # Populate unified runtime config and context from single source of truth
        from k8s_autopilot.model.reasoning import with_effort_model_params
        from k8s_autopilot.security.approval_mode import approval_mode_key

        appr_mode = telemetry.current_approval_mode
        live_key = telemetry.approval_mode_key
        model_params = with_effort_model_params(active_model, {}, active_effort)

        config["configurable"].update({
            "model": active_model,
            "reasoning_effort": active_effort,
            "approval_mode": appr_mode,
        })
        if live_key is not None:
            config["configurable"]["approval_mode_key"] = live_key
        else:
            config["configurable"].pop("approval_mode_key", None)

        raw_context: dict[str, Any] = {
            "model": active_model,
            "model_params": model_params,
            "reasoning_effort": active_effort,
            "thread_id": context_id,
            "turn_id": str(uuid.uuid4()),
            "approval_mode": appr_mode,
            "auto_approve": (appr_mode == "yolo"),
        }
        if live_key is not None:
            raw_context["approval_mode_key"] = live_key
        else:
            raw_context.pop("approval_mode_key", None)

        # Filter enriched_context strictly to fields accepted by context_schema
        schema_cls = getattr(agent_graph, "context_schema", None)
        if schema_cls is None:
            from k8s_autopilot.agent.config import CLIContextSchema

            schema_cls = CLIContextSchema

        if hasattr(schema_cls, "__dataclass_fields__"):
            valid_fields = set(schema_cls.__dataclass_fields__.keys())
            enriched_context: dict[str, Any] = {
                k: v for k, v in raw_context.items() if k in valid_fields
            }
        else:
            enriched_context = raw_context

        renderer = _StreamRenderer(updater, context_id, task.id)
        ctx = _StreamContext(
            task=task,
            updater=updater,
            event_queue=event_queue,
            context_id=context_id,
            use_ui=use_ui,
            renderer=renderer,
            telemetry=telemetry,
        )
        renderer.set_attach_trace(lambda msg_obj, event_type: self._attach_stream_trace(msg_obj, event_type, ctx))

        try:
            input_data = self._prepare_stream_input(query)
            interrupted = False

            stream_kwargs: dict[str, Any] = {
                "config": config,
                "stream_mode": ["messages", "updates", "custom"],
                "subgraphs": True,
            }
            import inspect

            try:
                sig = inspect.signature(agent_graph.astream)
                if "context" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    stream_kwargs["context"] = enriched_context
            except Exception:
                stream_kwargs["context"] = enriched_context

            stream = agent_graph.astream(input_data, **stream_kwargs)

            async for chunk in stream:
                if not isinstance(chunk, tuple):
                    continue
                if len(chunk) == 3:
                    namespace, mode, payload = chunk
                elif len(chunk) == 2:
                    mode, payload = chunk
                    namespace = ()
                else:
                    continue

                is_main_agent = (tuple(namespace) if namespace else ()) == ()

                if mode == "messages":
                    if not interrupted:
                        if is_main_agent:
                            await self._handle_messages_chunk(payload, ctx)
                        else:
                            await self._handle_subagent_messages_chunk(payload, tuple(namespace), ctx)
                elif mode == "updates":
                    if await self._handle_updates_chunk(payload, ctx):
                        interrupted = True
                elif mode == "custom":
                    await self._handle_custom_chunk(payload, ctx)

            if not interrupted:
                await self._finalize_stream_completion(ctx, agent_graph, config)

        except asyncio.CancelledError:
            logger.warning(
                "Task was cancelled by user",
                task_id=task.id,
                context_id=context_id,
                extra={"agent_name": agent_name},
            )
            msg_id = renderer.message_id if renderer else None
            try:
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
            except Exception:
                pass
            raise
        except GeneratorExit:
            logger.info(
                "Task stream closed / GeneratorExit",
                task_id=task.id,
                context_id=context_id,
                extra={"agent_name": agent_name},
            )
            return
        except Exception as e:
            logger.error(
                "Exception in agent stream: %s",
                e,
                task_id=task.id,
                context_id=context_id,
                exc_info=True,
                extra={"agent_name": agent_name},
            )
            await renderer.close_thinking()
            ctx.reasoning_surface_id = None
            ctx.reasoning_buffer = ""
            await self._flush_active_tool_surfaces(ctx)

            error_msg_str = str(e)
            if "404" in error_msg_str or "not found" in error_msg_str.lower():
                user_friendly_error = (
                    f"Model '{active_model}' was not found or is unavailable in your project/region. "
                    "Please select another model from the model selector to continue."
                )
            elif "401" in error_msg_str or "403" in error_msg_str or "permission" in error_msg_str.lower():
                user_friendly_error = (
                    f"Authentication or permission error when calling model '{active_model}'. "
                    "Please check your API key and project permissions."
                )
            elif "429" in error_msg_str or "quota" in error_msg_str.lower() or "rate" in error_msg_str.lower():
                user_friendly_error = (
                    f"Rate limit or quota exceeded for model '{active_model}'. "
                    "Please try again in a few moments or switch to a different model."
                )
            else:
                user_friendly_error = f"Agent execution encountered an error: {e}"

            msg_out = self._make_stream_message_text(
                user_friendly_error,
                renderer.message_id,
                context_id,
                task.id,
            )
            self._attach_stream_trace(msg_out, "error", ctx)
            await updater.update_status(TaskState.TASK_STATE_FAILED, msg_out)
            await self._safe_complete(updater, task)

    # ── Shared utilities ──────────────────────────────────────────────

    def _build_a2ui_parts(
        self,
        content: Any,
        status: str = "working",
        is_task_complete: bool = False,
        require_user_input: bool = False,
        response_type: str = "text",
        metadata: dict[str, Any] | None = None,
        use_ui: bool = True,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> list[Part]:
        """Build A2UI Parts using the component registry."""
        from k8s_autopilot.a2ui.registry import RenderContext, get_registry

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
            agent_name=getattr(self.agent, "name", "k8sAutopilotAgent"),
            metadata=meta,
            use_ui=use_ui,
            session_id=session_id,
            task_id=task_id,
        )
        return get_registry().build_parts(ctx)

    @staticmethod
    def _content_to_str(content: Any) -> str:
        """Convert content to a display-friendly string."""
        if isinstance(content, dict):
            parts = []
            if content.get("summary"):
                parts.append(str(content["summary"]).strip())
            if content.get("message"):
                parts.append(str(content["message"]).strip())
            if content.get("question"):
                parts.append(str(content["question"]).strip())

            if parts:
                return "\n\n".join(parts)
            return json.dumps(content, indent=2)
        return str(content) if content else "Processing..."

    def _attach_trace_metadata(
        self,
        msg: Message,
        run_id: str,
        step_index: int,
        event_type: str,
        approval_mode: str = "manual",
        goal_status: str | None = None,
        goal_objective: str | None = None,
        goal_rubric: str | None = None,
        rubric_active: bool = False,
        rubric_label: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        model: str = "",
        reasoning_effort: str = "",
    ) -> None:
        """Attach trace metadata to a ``Message`` for the reasoning panel and telemetry status bar."""
        agent_name = getattr(self.agent, "name", "k8sAutopilotAgent")
        if not isinstance(agent_name, str):
            agent_name = "k8sAutopilotAgent"

        trace_dict = {
            "run_id": run_id,
            "step_index": step_index,
            "action": event_type,
            "approval_mode": approval_mode or "manual",
            "goal_status": goal_status,
            "goal_objective": goal_objective,
            "goal_rubric": goal_rubric,
            "rubric_active": rubric_active,
            "rubric_label": rubric_label,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost_usd,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }

        msg.metadata.update({
            "traceRunId": run_id,
            "traceStepIndex": step_index,
            "agentName": agent_name,
            "eventType": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "approval_mode": approval_mode or "manual",
            "goal_status": goal_status or "",
            "goal_objective": goal_objective or "",
            "goal_rubric": goal_rubric or "",
            "rubric_active": rubric_active or False,
            "rubric_label": rubric_label or "",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost_usd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "trace": json.dumps(trace_dict),
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

    async def _handle_interrupt_event(
        self,
        interrupt_val: Any,
        use_ui: bool,
        renderer: _StreamRenderer,
        updater: TaskUpdater,
        context_id: str,
        task_id: str,
        run_id: str,
        step_index: int,
        approval_mode: str = "manual",
        goal_status: str | None = None,
        goal_objective: str | None = None,
        goal_rubric: str | None = None,
        rubric_active: bool = False,
        rubric_label: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        model: str = "",
        reasoning_effort: str = "",
    ) -> None:
        """Handle LangGraph interrupt events and emit appropriate A2UI or text prompts."""
        raw_item = (
            interrupt_val[0]
            if isinstance(interrupt_val, (list, tuple)) and interrupt_val
            else interrupt_val
        )
        raw_val = getattr(raw_item, "value", raw_item)
        if not isinstance(raw_val, dict):
            raw_val = {"value": raw_val}

        # 1. Ask User Interrupt
        if raw_val.get("type") == "ask_user" or "questions" in raw_val:
            questions_raw = raw_val.get("questions")
            questions = questions_raw if isinstance(questions_raw, list) else []
            if use_ui:
                surface_id = f"ask-user-{uuid.uuid4().hex[:8]}"
                ops = build_ask_user_surface(
                    surface_id=surface_id,
                    questions=questions,
                    action_id="ask_user_response",
                )
                parts = [create_a2ui_part(op) for op in ops]
                msg_out = self._make_stream_message_parts(
                    parts, renderer.message_id, context_id, task_id
                )
            else:
                question_text = ""
                for idx, q in enumerate(questions, 1):
                    if isinstance(q, dict):
                        q_str = str(q.get("question", ""))
                        choices = q.get("choices")
                        if isinstance(choices, list) and choices:
                            opts = ", ".join(
                                str(c.get("value", ""))
                                for c in choices
                                if isinstance(c, dict)
                            )
                            question_text += f"\n{idx}. {q_str} (Options: {opts})"
                        else:
                            question_text += f"\n{idx}. {q_str}"
                    elif isinstance(q, str):
                        question_text += f"\n{idx}. {q}"
                prompt_msg = (
                    f"User input requested:{question_text}"
                    if question_text
                    else "Please provide your input."
                )
                msg_out = self._make_stream_message_text(
                    prompt_msg.strip(),
                    renderer.message_id,
                    context_id,
                    task_id,
                )
            self._attach_trace_metadata(
                msg_out,
                run_id,
                step_index,
                "ask_user_interrupt",
                approval_mode=approval_mode,
                goal_status=goal_status,
                goal_objective=goal_objective,
                goal_rubric=goal_rubric,
                rubric_active=rubric_active,
                rubric_label=rubric_label,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            await updater.update_status(TaskState.TASK_STATE_INPUT_REQUIRED, msg_out)
            return

        # 2. HITL Tool Approval Interrupt
        action_requests = raw_val.get("action_requests") if isinstance(raw_val, dict) else None
        first_req = (
            action_requests[0]
            if isinstance(action_requests, list) and action_requests
            else raw_val
        ) if action_requests else None
        req_name = str(
            (first_req or {}).get("name")
            or (first_req or {}).get("action_type")
            or (first_req or {}).get("tool_name")
            or ""
        )

        if action_requests and req_name != "propose_goal":
            req_dict: dict[str, Any] = first_req if isinstance(first_req, dict) else {}
            action_type: str = str(
                req_dict.get("action_type")
                or req_dict.get("tool_name")
                or req_dict.get("name")
                or "Action Approval"
            )
            description: str = str(
                req_dict.get("description")
                or f"Execution of {action_type} requires approval."
            )
            justification: str = str(req_dict.get("justification") or "")
            risk_level: str = str(req_dict.get("risk_level") or "medium")
            params_list: list[dict[str, str]] = []
            tool_args = req_dict.get("args") or req_dict.get("parameters") or {}
            if isinstance(tool_args, dict):
                for k, v in tool_args.items():
                    params_list.append({"key": str(k), "value": str(v)[:200]})

            phase_val: str = str(raw_val.get("phase", "execution")) if isinstance(raw_val, dict) else "execution"

            logger.warning(
                "HITL interrupt triggered: waiting for human approval",
                task_id=task_id,
                context_id=context_id,
                extra={
                    "tool_name": action_type,
                    "decision_needed": description,
                    "risk_level": risk_level,
                },
            )

            if use_ui:
                surface_id = f"hitl-{uuid.uuid4().hex[:8]}"
                ops = build_hitl_approval_surface(
                    surface_id=surface_id,
                    proposed_action=description,
                    justification=justification
                    or f"Tool '{action_type}' requires confirmation before proceeding.",
                    risk_level=risk_level,
                    action_id="hitl_response",
                    phase=phase_val,
                    parameters=params_list,
                )
                parts = [create_a2ui_part(op) for op in ops]
                msg_out = self._make_stream_message_parts(
                    parts, renderer.message_id, context_id, task_id
                )
            else:
                msg_out = self._make_stream_message_text(
                    f"Approval required for: {description}\n{justification}".strip(),
                    renderer.message_id,
                    context_id,
                    task_id,
                )
            self._attach_trace_metadata(
                msg_out,
                run_id,
                step_index,
                "hitl_interrupt",
                approval_mode=approval_mode,
                goal_status=goal_status,
                goal_objective=goal_objective,
                goal_rubric=goal_rubric,
                rubric_active=rubric_active,
                rubric_label=rubric_label,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            await updater.update_status(TaskState.TASK_STATE_INPUT_REQUIRED, msg_out)
            return

        # 3. Goal Review / Confirmation Interrupt
        if (
            raw_val.get("type") in ("goal_review", "goal_confirmation", "goal_criteria")
            or "goal_review" in raw_val
            or ("objective" in raw_val and "criteria" in raw_val)
            or ("goalText" in raw_val and "criteria" in raw_val)
            or (action_requests and req_name == "propose_goal")
        ):
            if action_requests and req_name == "propose_goal":
                first_req_dict = first_req if isinstance(first_req, dict) else {}
                tool_args_raw = first_req_dict.get("args") or first_req_dict.get("parameters") or {}
                tool_args = tool_args_raw if isinstance(tool_args_raw, dict) else {}
                goal_text = str(
                    tool_args.get("objective")
                    or tool_args.get("goal")
                    or tool_args.get("goalText")
                    or "Goal Review"
                )
                criteria_raw = tool_args.get("criteria", [])
            else:
                goal_obj = raw_val.get("goal") or raw_val.get("goal_review") or raw_val
                goal_text = ""
                criteria_raw = []
                if isinstance(goal_obj, dict):
                    goal_text = str(
                        goal_obj.get("objective")
                        or goal_obj.get("goal")
                        or goal_obj.get("goalText")
                        or goal_obj.get("goal_text")
                        or "Goal Review"
                    )
                    criteria_raw = goal_obj.get("criteria", [])
                else:
                    goal_text = str(goal_obj)

            if isinstance(criteria_raw, list):
                criteria_list = [str(c).strip() for c in criteria_raw if str(c).strip()]
            elif isinstance(criteria_raw, str):
                criteria_list = [
                    line.strip().lstrip("-* ").strip()
                    for line in criteria_raw.splitlines()
                    if line.strip().lstrip("-* ").strip()
                ]
            else:
                criteria_list = []

            if use_ui:
                surface_id = f"goal-{uuid.uuid4().hex[:8]}"
                ops = build_goal_confirmation_surface(
                    surface_id=surface_id,
                    goal_text=goal_text or "Goal Confirmation",
                    criteria=criteria_list,
                    action_id="goal_response",
                )
                parts = [create_a2ui_part(op) for op in ops]
                msg_out = self._make_stream_message_parts(
                    parts, renderer.message_id, context_id, task_id
                )
            else:
                criteria_text = "\n".join(f"- {c}" for c in criteria_list)
                prompt_msg = (
                    f"Goal Review Required:\n**Goal:** {goal_text}\n\n"
                    f"**Criteria:**\n{criteria_text}\n\n"
                    "Please confirm, edit, reject with feedback, or dismiss."
                )
                msg_out = self._make_stream_message_text(
                    prompt_msg.strip(),
                    renderer.message_id,
                    context_id,
                    task_id,
                )
            self._attach_trace_metadata(
                msg_out,
                run_id,
                step_index,
                "goal_interrupt",
                approval_mode=approval_mode,
                goal_status=goal_status,
                goal_objective=goal_objective,
                goal_rubric=goal_rubric,
                rubric_active=rubric_active,
                rubric_label=rubric_label,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            await updater.update_status(TaskState.TASK_STATE_INPUT_REQUIRED, msg_out)
            return

        # 4. Generic Interrupt Fallback
        if use_ui:
            parts = self._build_a2ui_parts(
                content=raw_val,
                status="input_required",
                is_task_complete=False,
                require_user_input=True,
                metadata=raw_val,
                use_ui=use_ui,
                session_id=context_id,
                task_id=task_id,
            )
            msg_out = self._make_stream_message_parts(
                parts, renderer.message_id, context_id, task_id
            )
        else:
            msg_out = self._make_stream_message_text(
                self._content_to_str(raw_val),
                renderer.message_id,
                context_id,
                task_id,
            )
        self._attach_trace_metadata(
            msg_out,
            run_id,
            step_index,
            "interrupt",
            approval_mode=approval_mode,
            goal_status=goal_status,
            rubric_active=rubric_active,
            rubric_label=rubric_label,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        await updater.update_status(TaskState.TASK_STATE_INPUT_REQUIRED, msg_out)


    def _make_stream_message_text(
        self, text: str, message_id: str | None, context_id: str, task_id: str
    ) -> Message:
        """Create a plain text streaming message with a stable ID."""
        if not message_id:
            return new_text_message(
                text, context_id=context_id, task_id=task_id, role=Role.ROLE_AGENT
            )
        return Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text=text)],
            message_id=message_id,
            context_id=context_id,
            task_id=task_id,
        )

    def _make_stream_message_parts(
        self, parts: Sequence[Part], message_id: str | None, context_id: str, task_id: str
    ) -> Message:
        """Create a parts-based message with a stable ID."""
        if not message_id:
            return new_message(
                list(parts), context_id=context_id, task_id=task_id, role=Role.ROLE_AGENT
            )
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
            logger.info("Agent stream completed successfully", task_id=task.id)
        except RuntimeError as e:
            if "already in a terminal state" in str(e):
                logger.info(
                    "Task already terminal, skipping complete()",
                    task_id=task.id,
                )
            else:
                raise
