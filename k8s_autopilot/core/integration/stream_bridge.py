"""Bridge between LangGraph graph execution and messaging platforms.

Uses ``stream_events(version="v3")`` — the official recommended
streaming API from LangGraph docs — with typed projections:

- ``stream.messages``    → token-by-token LLM output
- ``stream.interrupts``  → HITL approval requests
- ``stream.output``      → final graph state
- ``stream.subgraphs``   → deep-agent / coordinator activity

The bridge is agent-agnostic and platform-agnostic.  Platform-specific
output is handled by the :class:`StreamSink` protocol.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import (
    HumanMessage as LCHumanMessage,
    SystemMessage as LCSystemMessage,
    AIMessage as LCAIMessage,
    AIMessageChunk as LCAIMessageChunk,
)
from langgraph.types import Command

from k8s_autopilot.core.integration.content_filter import (
    is_internal_message,
)
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("StreamBridge")


def _humanize_tool_name(raw_name: str) -> str:
    """Convert ``snake_case`` tool name to *Title Case* display name.

    Strips common prefixes (``kubernetes_``, ``kubectl_``, ``helm_``, ``k8s_``)
    so that ``kubernetes_get_helm_releases`` becomes ``Get Helm Releases``.
    """
    if not raw_name:
        return "Tool"
    name = raw_name
    for prefix in ("transfer_to_", "kubernetes_", "kubectl_", "k8s_", "helm_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", " ").strip().title() or raw_name.title()


# ---------------------------------------------------------------------------
# StreamSink protocol — platform adapters implement this
# ---------------------------------------------------------------------------


@runtime_checkable
class StreamSink(Protocol):
    """Protocol for platform-specific stream output handlers.

    Implementations translate generic streaming events into
    platform-native API calls (e.g. Slack ``chat.startStream`` /
    ``chat.appendStream`` / ``chat.stopStream``).
    """

    async def on_stream_start(
        self,
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Called once when a new streaming response begins."""
        ...

    async def on_token(self, token: str) -> None:
        """Called for each LLM output token."""
        ...

    async def on_stream_end(self) -> None:
        """Called when the graph run completes without interrupts."""
        ...

    async def on_interrupt(self, interrupt_value: dict[str, Any]) -> None:
        """Called when the graph hits an ``interrupt()`` (HITL gate).

        The *interrupt_value* is the payload passed to
        ``interrupt({...})`` in the graph node — typically contains
        ``question``, ``details``, ``action_requests``, etc.
        """
        ...

    async def on_error(self, error: Exception) -> None:
        """Called when the graph run raises an exception."""
        ...

    async def on_pre_interrupt(self) -> None:
        """Called just before ``on_interrupt`` — lets the sink capture
        any buffered text so it can be included as context in the
        interrupt UI (e.g. plan text for approval cards).

        Optional — default implementation does nothing.
        """
        ...

    async def on_subgraph_event(
        self,
        namespace: tuple[str, ...],
        data: dict[str, Any],
    ) -> None:
        """Called for deep-agent / coordinator subgraph activity.

        Optional — implementations may choose to ignore subgraph events.
        """
        ...

    async def on_thinking_step(self, step_text: str) -> None:
        """Called for intermediate agent activity — tool calls, results,
        delegation labels.

        Optional — implementations may choose to ignore thinking steps.
        """
        ...


# ---------------------------------------------------------------------------
# GraphStreamBridge — the main orchestrator
# ---------------------------------------------------------------------------


class GraphStreamBridge:
    """Streams a LangGraph graph run through a :class:`StreamSink`.

    This class is the core adapter between LangGraph's execution engine
    and any messaging platform.  It is completely agent-agnostic —
    pass any compiled LangGraph graph and any ``StreamSink``
    implementation.

    Usage::

        bridge = GraphStreamBridge(graph=supervisor.graph)
        await bridge.stream_run(sink, input_state, config, channel, thread)
        # or, to resume after a HITL interrupt:
        await bridge.resume_run(sink, resume_value, config, channel, thread)
    """

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    # ── Primary run ───────────────────────────────────────────────────

    async def stream_run(
        self,
        sink: StreamSink,
        input_state: dict[str, Any],
        config: dict[str, Any],
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Execute a graph run, streaming output through *sink*.

        Uses ``graph.stream_events(input_state, version="v3")`` with
        typed projections.
        """
        await self._execute_stream(
            sink=sink,
            graph_input=input_state,
            config=config,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    # ── Resume after interrupt ────────────────────────────────────────

    async def resume_run(
        self,
        sink: StreamSink,
        resume_value: Any,
        config: dict[str, Any],
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Resume an interrupted graph with a human decision.

        Sends ``Command(resume=resume_value)`` which becomes the return
        value of the ``interrupt()`` call inside the paused node.
        """
        await self._execute_stream(
            sink=sink,
            graph_input=Command(resume=resume_value),
            config=config,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    # ── Internal streaming loop ───────────────────────────────────────

    async def _execute_stream(
        self,
        sink: StreamSink,
        graph_input: Any,
        config: dict[str, Any],
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Streams graph execution through a :class:`StreamSink`.

        Uses ``astream(stream_mode=["messages", "updates", "custom"])``
        which yields tuples of ``(mode, chunk)``:

        - ``messages``: LLM token chunks from top-level nodes
        - ``updates``:  Node state updates + ``__interrupt__``
        - ``custom``:   ``StreamWriter`` payloads from coordinator nodes
        """
        _INTERNAL_NODES = frozenset({
            "classify_request",
            "supervisor_router",
            "error_handler",
            "finalize_response",
        })

        # Human-readable labels for coordinator nodes
        _NODE_LABELS: dict[str, str] = {
            "helm_agent": "Helm Operator",
            "k8s_ops_agent": "K8s Operator",
            "app_mgmt_agent": "App Operator",
            "observability_agent": "Observability",
        }

        # Internal tools that should not be surfaced as thinking steps
        _INTERNAL_TOOLS = frozenset({
            "task",
            "request_chat_continue",
            "request_user_input",
            "transfer_to_helm_operator",
            "transfer_to_k8s_operator",
            "transfer_to_app_operator",
            "transfer_to_observability_operator",
        })

        # Thinking block types (Gemini, Anthropic, OpenAI)
        _THINKING_TYPES = frozenset({"thinking", "thought", "reasoning"})
        _THINKING_KEYS = ("thinking", "thought", "reasoning", "text")

        # Track which deep-agent tools we've already announced
        _announced_tools: set[str] = set()

        try:
            await sink.on_stream_start(channel_id, thread_id)

            try:
                async for mode, chunk in self.graph.astream(
                    graph_input,
                    config=config,
                    stream_mode=["messages", "updates", "custom"],
                ):
                    # ── Top-level LLM messages ────────────────────────
                    if mode == "messages":
                        msg_chunk, metadata = chunk
                        node = metadata.get("langgraph_node", "")
                        if node in _INTERNAL_NODES:
                            continue

                        # Skip complete messages (non-chunks) yielded via state updates
                        # to prevent duplicating already-streamed text.
                        if isinstance(msg_chunk, LCAIMessage) and not isinstance(msg_chunk, LCAIMessageChunk):
                            continue

                        # Skip SystemMessage echoes — these are middleware
                        # injections (PlanLockMiddleware, etc.) that the
                        # LLM may echo back.  Never surface to users.
                        if isinstance(msg_chunk, LCSystemMessage):
                            continue

                        # Skip HumanMessage echoes — coordinator nodes
                        # return HumanMessage(content=final_msg) in their
                        # state update, which re-emits already-streamed
                        # deep-agent text via the top-level messages stream.
                        if isinstance(msg_chunk, LCHumanMessage):
                            continue

                        # Tool call chunks → thinking step
                        tool_call_chunks = getattr(msg_chunk, "tool_call_chunks", None)
                        if tool_call_chunks:
                            for tc in tool_call_chunks:
                                tc_name = tc.get("name") or tc.get("function", {}).get("name", "")
                                if tc_name and tc_name not in _INTERNAL_TOOLS:
                                    await sink.on_thinking_step(
                                        f"Calling: {_humanize_tool_name(tc_name)}"
                                    )
                            continue

                        # ToolMessage results → thinking step
                        msg_type = getattr(msg_chunk, "type", "")
                        if msg_type == "tool":
                            continue  # Skip tool results for top-level

                        # Content blocks (thinking only — final text is handled via fallback)
                        if hasattr(msg_chunk, "content"):
                            content = msg_chunk.content
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict):
                                        btype = block.get("type", "")
                                        if btype in _THINKING_TYPES:
                                            # Extract thinking text
                                            text = next(
                                                (block[k] for k in _THINKING_KEYS if block.get(k)),
                                                "",
                                            )
                                            if text:
                                                await sink.on_thinking_step(text)

                    # ── Custom stream events (deep-agent forwarding) ──
                    elif mode == "custom":
                        if not isinstance(chunk, dict):
                            continue
                        kind = chunk.get("kind", "")
                        if kind != "deep_agent_message":
                            logger.debug(f"Skipping non-deep_agent custom chunk: kind={kind}")
                            continue

                        agent_node = chunk.get("node", "")
                        msg_data = chunk.get("data")
                        if msg_data is None:
                            continue

                        # logger.info(
                        #     f"deep_agent_message received | node={agent_node} data_type={type(msg_data).__name__}"
                        # )

                        # msg_data is (AIMessageChunk, metadata) tuple
                        msg_chunk: Any
                        metadata: Any
                        if isinstance(msg_data, (list, tuple)) and len(msg_data) == 2:
                            msg_chunk, metadata = msg_data[0], msg_data[1]
                        else:
                            msg_chunk = msg_data
                            metadata = {}

                        # Deep-agent tool calls → thinking step
                        tool_call_chunks = getattr(msg_chunk, "tool_call_chunks", None)
                        if tool_call_chunks:
                            for tc in tool_call_chunks:
                                tc_name = tc.get("name") or tc.get("function", {}).get("name", "")
                                if tc_name and tc_name not in _INTERNAL_TOOLS and tc_name not in _announced_tools:
                                    _announced_tools.add(tc_name)
                                    label = _NODE_LABELS.get(agent_node, agent_node)
                                    await sink.on_thinking_step(
                                        f"{label} → {_humanize_tool_name(tc_name)}"
                                    )
                            continue

                        # Deep-agent tool results → skip (already announced)
                        msg_type = getattr(msg_chunk, "type", "")
                        if msg_type == "tool":
                            continue

                        # Deep-agent content (thinking only)
                        if hasattr(msg_chunk, "content"):
                            content = msg_chunk.content
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict):
                                        btype = block.get("type", "")
                                        if btype in _THINKING_TYPES:
                                            text = next(
                                                (block[k] for k in _THINKING_KEYS if block.get(k)),
                                                "",
                                            )
                                            if text:
                                                await sink.on_thinking_step(text)

                    # ── State updates + interrupts ────────────────────
                    elif mode == "updates":
                        for node_name in chunk:
                            if node_name.startswith("__"):
                                continue
                            if node_name in _INTERNAL_NODES:
                                continue
                            label = _NODE_LABELS.get(node_name, node_name)
                            await sink.on_thinking_step(
                                f"Processing — {label}"
                            )

                        if "__interrupt__" in chunk:
                            interrupts = chunk["__interrupt__"]
                            # Let sink capture any accumulated text
                            # before we dispatch the interrupt.
                            await sink.on_pre_interrupt()
                            for intr in interrupts:
                                value = getattr(intr, "value", intr)
                                if isinstance(value, dict):
                                    pfr = value.get("pending_feedback_requests", {})
                                    question = pfr.get("question", "")
                                    if question:
                                        value["question"] = question
                                    await sink.on_interrupt(value)
                            # on_interrupt stops the stream, so break
                            break
                    
                else:
                    # If we didn't break from an interrupt, cleanly end
                    await sink.on_stream_end()

            except Exception as exc:
                # Fallback if any internal GraphInterrupt slips through
                exc_type = type(exc).__name__
                if exc_type == "GraphInterrupt":
                    interrupts = getattr(exc, "interrupts", [])
                    for intr in interrupts:
                        value = getattr(intr, "value", intr)
                        if isinstance(value, dict):
                            await sink.on_interrupt(value)
                    await sink.on_stream_end()
                    return
                raise

        except Exception as exc:
            logger.error(
                f"Graph stream error: {exc}",
            )
            await sink.on_error(exc)

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Extract plain text from various LLM content formats."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("text"):
                    parts.append(block["text"])
            return "".join(parts)
        return str(content) if content else ""
