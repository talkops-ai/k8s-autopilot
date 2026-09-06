# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportCallIssue=false
"""Slack-specific implementation of the ``MessagingIntegration`` base class.

Orchestrates the full pipeline:

1. Bolt event → :class:`IncomingMessage`
2. Identity resolution → :class:`UserIdentity` (RBAC context)
3. Thread mapping → LangGraph ``thread_id``
4. Build graph input state (compatible with ``MainSupervisorState``)
5. Stream graph execution → :class:`SlackStreamSink` → Slack UI
6. Handle HITL ``interrupt()`` → Block Kit approval → ``Command(resume=...)``
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
import time
from collections import OrderedDict
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from slack_bolt.async_app import AsyncApp  # type: ignore[import-not-found]
from slack_sdk.web.async_client import AsyncWebClient  # type: ignore[import-not-found]

from k8s_autopilot.config.settings import Settings, get_settings
from k8s_autopilot.integrations.base import (
    IncomingMessage,
    InteractionPayload,
    MessagingIntegration,
)
from k8s_autopilot.integrations.identity import IdentityMapper
from k8s_autopilot.integrations.stream_bridge import GraphStreamBridge
from k8s_autopilot.integrations.thread_store import ThreadStore
from k8s_autopilot.integrations.slack.mrkdwn import md_to_mrkdwn
from k8s_autopilot.integrations.slack.operation_gate import SlackOperationGate
from k8s_autopilot.integrations.slack.stream_sink import SlackStreamSink

logger = AgentLogger("SlackIntegration")


class SlackMessagingIntegration(MessagingIntegration):
    """Slack implementation that bridges Bolt events to LangGraph graph runs."""

    def __init__(
        self,
        config: Settings | None,
        bolt_app: AsyncApp,
        graph: Any,
    ) -> None:
        self.config = config
        self.bolt_app = bolt_app
        self.client = AsyncWebClient(token=config.SLACK_BOT_TOKEN)
        self.stream_bridge = GraphStreamBridge(graph=graph)
        self.thread_store = ThreadStore()
        self.identity_mapper = IdentityMapper()
        # Operation gate — classifies read/write before graph invocation
        self.operation_gate = SlackOperationGate(config)
        # Optional: per-thread context from assistant_thread_context_changed
        self._thread_contexts: dict[str, dict[str, Any]] = {}
        # Tracks threads that are awaiting a text reply (for chat_continue,
        # pending_feedback_requests, and plain question interrupts).
        # Key: Slack thread_ts, Value: LangGraph thread_id
        self._pending_text_replies: dict[str, str] = {}
        # ── Event deduplication ─────────────────────────────────────
        # Slack retries event delivery if the initial HTTP response is
        # delayed.  Without dedup, the same message gets processed
        # multiple times, causing duplicate approval cards.  We track
        # processed events keyed by ``channel:message_ts`` with a TTL
        # window.  Entries older than the window are evicted lazily.
        self._processed_events: OrderedDict[str, float] = OrderedDict()
        self._dedup_window_secs: float = 60.0
        self._max_dedup_entries: int = 2000

    # ------------------------------------------------------------------
    # MessagingIntegration interface
    # ------------------------------------------------------------------

    async def handle_message(self, message: IncomingMessage) -> None:
        """Process an incoming Slack message through the LangGraph agent.

        Steps:

        0. Deduplicate retried Slack events
        1. Resolve user identity → RBAC permissions
        2. Map Slack thread → LangGraph thread_id
        3. Build ``MainSupervisorState``-compatible input
        4. Stream execution via ``SlackStreamSink``
        """
        # 0. Deduplication — Slack retries if the HTTP 200 is delayed.
        #    Skip events we've already started processing.
        if self._is_duplicate_event(message):
            return

        # 0b. Operation gate — classify read/write before invoking graph.
        #
        #     Runs on EVERY message entering handle_message(), including
        #     chat_continue free-form text replies.  This is critical because
        #     chat_continue lets users pivot to write operations mid-thread
        #     (e.g. "list apps" → agent asks "anything else?" → "modify the
        #     nginx app").
        #
        #     Button-click HITL flows (approve/reject/edit) are handled by
        #     Bolt action handlers in bolt_handlers.py and NEVER reach
        #     handle_message(), so they are unaffected by this gate.
        gate_result = await self.operation_gate.classify(message.text)
        if not gate_result.allowed:
            logger.info(
                "Write operation blocked by operation gate",
                extra={
                    "user": message.user_id,
                    "agent": (
                        gate_result.classification.agent
                        if gate_result.classification
                        else "unknown"
                    ),
                    "reasoning": (
                        gate_result.classification.reasoning
                        if gate_result.classification
                        else ""
                    ),
                },
            )
            # NOTE: Do NOT clean up _pending_text_replies here.
            # The graph is still paused at the chat_continue interrupt
            # with full conversational context (e.g. which apps were listed).
            # The user's next read message will resume the graph via
            # Command(resume=...) and the agent will retain that context.
            await self.send_text(
                channel_id=message.channel_id,
                thread_id=message.thread_id,
                text=md_to_mrkdwn(
                    gate_result.block_message
                    or "This operation is not available via Slack.",
                ),
            )
            return

        # 1. Resolve identity & RBAC
        identity = self.identity_mapper.resolve("slack", message.user_id)

        # 2. Map thread
        lg_thread_id = self.thread_store.resolve("slack", message.thread_id)

        # 3. Build graph input state
        input_state = self._build_input_state(message, identity, lg_thread_id)

        # 4. Create Slack stream sink
        sink = SlackStreamSink(
            client=self.client,
            channel_id=message.channel_id,
            thread_ts=message.thread_id,
            config=self.config,
        )

        # 4a. Check if this thread is awaiting a text reply (HITL resume)
        if message.thread_id in self._pending_text_replies:
            lg_thread_id_resume = self._pending_text_replies[message.thread_id]
            graph_config = {
                "configurable": {
                    "thread_id": lg_thread_id_resume,
                    "app_config": self.config,
                }
            }

            # ── Validate that the graph actually has an active interrupt.
            # If the previous run completed and the registration is stale,
            # fall through to the fresh stream_run path instead of sending
            # a Command(resume=...) into a finished graph (which produces
            # zero output because supervisor_router → finalize_response).
            has_active_interrupt = False
            state = None
            try:
                state = await self.stream_bridge.graph.aget_state(graph_config)
                has_active_interrupt = bool(state.next) and any(
                    getattr(task, "interrupts", ())
                    for task in (state.tasks or ())
                )
            except Exception as exc:
                logger.warning(
                    f"Could not validate interrupt state: {exc}"
                )

            if not has_active_interrupt:
                # Graph completed — stale registration.  Remove and treat
                # this as a fresh request.
                self._pending_text_replies.pop(message.thread_id, None)
                logger.info(
                    "Stale text-reply registration cleared "
                    "— treating as fresh request",
                    extra={
                        "slack_thread": message.thread_id,
                        "lg_thread": lg_thread_id_resume,
                    },
                )
                # Fall through to the stream_run path below (step 5)
            else:
                # Graph IS interrupted — pop the entry and resume.
                self._pending_text_replies.pop(message.thread_id)

                # Convert text to decision payload if it came from action_requests
                resume_value = self._parse_text_reply_decision(message.text, message.user_id, state)

                logger.info(
                    "Resuming graph with text reply",
                    extra={
                        "slack_user": message.user_id,
                        "lg_thread": lg_thread_id_resume,
                        "text_preview": message.text[:50],
                    },
                )

                await self.stream_bridge.resume_run(
                    sink=sink,
                    resume_value=resume_value,
                    config=graph_config,
                    channel_id=message.channel_id,
                    thread_id=message.thread_id,
                )
                
                await self._post_fallback_response_if_needed(sink, graph_config, message.channel_id, message.thread_id)

                # Track if the resumed run also awaits text reply
                await self._track_pending_reply(
                    sink, graph_config, message.thread_id, lg_thread_id_resume,
                )
                return

        # 5. Stream graph → Slack
        graph_config = {
            "configurable": {
                "thread_id": lg_thread_id,
                "app_config": self.config,
            }
        }

        logger.info(
            "Starting graph run for Slack message",
            extra={
                "slack_user": message.user_id,
                "slack_channel": message.channel_id,
                "lg_thread": lg_thread_id,
                "rbac_role": identity.rbac_role,
            },
        )

        await self.stream_bridge.stream_run(
            sink=sink,
            input_state=input_state,
            config=graph_config,
            channel_id=message.channel_id,
            thread_id=message.thread_id,
        )
        
        await self._post_fallback_response_if_needed(sink, graph_config, message.channel_id, message.thread_id)

        # Track if the run ended with a text-reply interrupt
        await self._track_pending_reply(
            sink, graph_config, message.thread_id, lg_thread_id,
        )

    async def handle_interaction(self, payload: InteractionPayload) -> None:
        """Resume an interrupted graph run with the user's decision.

        Converts the Slack Block Kit approve/reject/edit/option action
        into a ``Command(resume=...)`` compatible with the HITL subsystem.

        Decision types per official LangGraph HITL docs:
        - ``{type: "approve"}``
        - ``{type: "reject", message: "..."}``
        - ``{type: "edit", editedAction: ...}`` (or ``{type: "respond", message: "..."}``)
        """
        lg_thread_id = self.thread_store.resolve("slack", payload.thread_id)

        # Build resume value based on action
        if payload.value == "approve":
            resume_value = {
                "decisions": [
                    {
                        "type": "approve",
                        "approved_by": payload.user_id,
                    },
                ],
            }
        elif payload.value == "edit":
            # Edit flow: extract modified text from modal submission
            edit_text = payload.raw_payload.get("edit_text", "")
            resume_value = {
                "decisions": [
                    {
                        "type": "edit",
                        "editedAction": edit_text,
                        "message": edit_text,
                        "edited_by": payload.user_id,
                    },
                ],
            }
        elif payload.value == "reject":
            resume_value = {
                "decisions": [
                    {
                        "type": "reject",
                        "rejected_by": payload.user_id,
                        "message": "Rejected by user via Slack",
                    },
                ],
            }
        else:
            # User input option selected — resume with the option value
            resume_value = payload.value

        # Create sink and resume
        sink = SlackStreamSink(
            client=self.client,
            channel_id=payload.channel_id,
            thread_ts=payload.thread_id,
            config=self.config,
        )

        graph_config = {
            "configurable": {
                "thread_id": lg_thread_id,
                "app_config": self.config,
            }
        }

        logger.info(
            "Resuming graph with HITL decision",
            extra={
                "action": payload.value,
                "user": payload.user_id,
                "lg_thread": lg_thread_id,
            },
        )

        await self.stream_bridge.resume_run(
            sink=sink,
            resume_value=resume_value,
            config=graph_config,
            channel_id=payload.channel_id,
            thread_id=payload.thread_id,
        )
        
        await self._post_fallback_response_if_needed(sink, graph_config, payload.channel_id, payload.thread_id)

        # Track if the resumed run also awaits text reply
        await self._track_pending_reply(
            sink, graph_config, payload.thread_id, lg_thread_id,
        )

    async def send_text(
        self,
        channel_id: str,
        thread_id: str,
        text: str,
    ) -> None:
        """Send a plain text message to Slack."""
        await self.client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_id,
            text=text,
        )

    async def send_blocks(
        self,
        channel_id: str,
        thread_id: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """Send rich Block Kit content to Slack."""
        await self.client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_id,
            blocks=blocks,
            text="",  # Fallback text (required by Slack API)
        )

    async def set_typing_status(
        self,
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Show typing indicator in Slack.

        Uses the assistant API ``setStatus`` if available, otherwise
        falls back to a no-op.
        """
        # Typing status is set via Bolt's `set_status` in bolt_handlers.py
        # This method is here for the abstract interface compliance.

    # ------------------------------------------------------------------
    # Additional Slack-specific methods
    # ------------------------------------------------------------------

    async def update_thread_context(
        self,
        channel_id: str,
        thread_ts: str,
        new_context: dict[str, Any],
    ) -> None:
        """Update the stored context for a Slack assistant thread.

        Called when ``assistant_thread_context_changed`` fires (user
        navigates to a different channel while the assistant is open).
        """
        key = f"{channel_id}:{thread_ts}"
        self._thread_contexts[key] = new_context

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _track_pending_reply(
        self,
        sink: SlackStreamSink,
        graph_config: dict[str, Any],
        slack_thread_id: str,
        lg_thread_id: str,
    ) -> None:
        """Register a thread for text-reply routing if needed.

        Checks two sources:
        1. ``sink.awaiting_text_reply`` — set by the sink's interrupt
           handler for chat_continue / feedback / plain question.
        2. ``graph.aget_state()`` — if the graph is **actually paused**
           (``state.next`` is non-empty AND ``state.tasks`` has
           interrupt objects), the coordinator completed without the
           sink seeing an interrupt (e.g., deep agent returned text
           directly after Modify click).  We still need to route the
           next user message as a resume.

        The dual check (``state.next`` + ``state.tasks``) prevents
        false positives: ``state.tasks`` can retain stale interrupt
        metadata from already-consumed interrupts, while ``state.next``
        is empty once the graph reaches a terminal node.
        """
        if sink.awaiting_text_reply:
            self._pending_text_replies[slack_thread_id] = lg_thread_id
            logger.info(
                "Thread registered for text reply (via sink)",
                extra={"slack_thread": slack_thread_id, "lg_thread": lg_thread_id},
            )
            return

        # Check graph state for pending interrupts not seen by the sink.
        # Require BOTH conditions to prevent stale registrations:
        #   • state.next is non-empty (graph is paused at a node)
        #   • state.tasks has interrupt objects (the pause is an interrupt)
        try:
            state = await self.stream_bridge.graph.aget_state(graph_config)
            is_graph_paused = bool(state.next)
            has_pending_interrupts = any(
                getattr(task, "interrupts", ())
                for task in (state.tasks or ())
            )
            if is_graph_paused and has_pending_interrupts:
                self._pending_text_replies[slack_thread_id] = lg_thread_id
                logger.info(
                    "Thread registered for text reply (via graph interrupts)",
                    extra={
                        "slack_thread": slack_thread_id,
                        "lg_thread": lg_thread_id,
                        "pending_nodes": list(state.next),
                    },
                )
            else:
                logger.debug(
                    f"Graph completed normally, no active interrupts"
                    f" | next={list(state.next)}"
                    f" paused={is_graph_paused}"
                    f" has_interrupts={has_pending_interrupts}"
                )
        except Exception as exc:
            logger.debug(f"Could not check graph state: {exc}")

    async def _post_fallback_response_if_needed(
        self,
        sink: SlackStreamSink,
        graph_config: dict[str, Any],
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Check if any text was streamed to Slack. If not, and the graph completed, post the final AIMessage."""
        if getattr(sink, "has_streamed", True):
            return

        try:
            state = await self.graph.aget_state(graph_config)
            # Only send fallback if the graph has completed (no next active nodes)
            if not state.next:
                messages = state.values.get("messages", [])
                if messages:
                    last_msg = messages[-1]
                    msg_type = getattr(last_msg, "type", None) or (
                        last_msg.get("type") if isinstance(last_msg, dict) else None
                    )
                    if msg_type == "ai":
                        content = getattr(last_msg, "content", None) or (
                            last_msg.get("content") if isinstance(last_msg, dict) else None
                        )
                        text_blocks = []
                        if isinstance(content, str):
                            text_blocks.append(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, str):
                                    text_blocks.append(block)
                                elif isinstance(block, dict):
                                    if block.get("type") == "text" and block.get("text"):
                                        text_blocks.append(block["text"])
                        
                        final_text = "".join(text_blocks).strip()
                        if final_text:
                            await self.send_text(
                                channel_id=channel_id,
                                thread_id=thread_id,
                                text=md_to_mrkdwn(final_text),
                            )
                            logger.info("Sent non-streaming fallback AI response to Slack", extra={"text_preview": final_text[:50]})
        except Exception as exc:
            logger.warning(f"Failed to check final state for fallback response: {exc}")

    def _parse_text_reply_decision(self, text: str, user_id: str, state: Any) -> Any:
        """Parse user text reply and convert it to a structured decision payload if the active interrupt is an action request."""
        try:
            for task in getattr(state, "tasks", ()) if state else ():
                for interrupt in getattr(task, "interrupts", ()):
                    val = interrupt.value
                    if isinstance(val, dict) and "action_requests" in val:
                        text_lower = text.strip().lower()
                        if text_lower in ("approve", "yes", "y", "ok", "go", "proceed", "go ahead"):
                            return {"decisions": [{"type": "approve", "approved_by": user_id}]}
                        elif text_lower in ("reject", "no", "n", "cancel", "stop", "abort"):
                            return {"decisions": [{"type": "reject", "rejected_by": user_id, "message": "Rejected via text reply"}]}
                        else:
                            return {"decisions": [{"type": "edit", "editedAction": text, "message": text, "edited_by": user_id}]}
        except Exception as e:
            logger.warning(f"Failed to inspect interrupt state for text reply formatting: {e}")
        return text

    # ------------------------------------------------------------------
    # Event deduplication helpers
    # ------------------------------------------------------------------

    def _is_duplicate_event(self, message: IncomingMessage) -> bool:
        """Return True if this event was already processed (Slack retry).

        Slack retries event delivery from multiple CDN nodes if the
        initial HTTP 200 is delayed beyond ~3 seconds.  Even though
        Bolt acks immediately, the graph run is async and may still be
        in progress when the retry arrives.  Without this gate, the
        same user message would start multiple concurrent graph runs,
        producing duplicate approval cards, duplicate streaming
        responses, and corrupted checkpointed state.

        Keying: ``channel_id:message_ts`` — ``ts`` is unique per
        channel in Slack and immutable across retries.
        """
        raw = message.raw_event or {}
        event_ts = raw.get("ts", "")
        if not event_ts:
            # No ts available — can't dedup, allow through.
            return False

        dedup_key = f"{message.channel_id}:{event_ts}"
        now = time.monotonic()

        if dedup_key in self._processed_events:
            logger.info(
                "Duplicate Slack event skipped",
                extra={
                    "dedup_key": dedup_key,
                    "user": message.user_id,
                    "text_preview": message.text[:40],
                },
            )
            return True

        # Record this event
        self._processed_events[dedup_key] = now

        # Lazy eviction of stale entries
        self._evict_stale_events(now)

        return False

    def _evict_stale_events(self, now: float | None = None) -> None:
        """Remove dedup entries older than ``_dedup_window_secs``.

        Also caps the dict at ``_max_dedup_entries`` to bound memory.
        """
        if now is None:
            now = time.monotonic()

        cutoff = now - self._dedup_window_secs

        # Evict expired entries (OrderedDict maintains insertion order)
        while self._processed_events:
            key, ts = next(iter(self._processed_events.items()))
            if ts < cutoff:
                self._processed_events.pop(key)
            else:
                break  # Remaining entries are newer

        # Hard cap — drop oldest if we exceed the limit
        while len(self._processed_events) > self._max_dedup_entries:
            self._processed_events.popitem(last=False)

    # ------------------------------------------------------------------
    # Input state builder
    # ------------------------------------------------------------------

    def _build_input_state(
        self,
        message: IncomingMessage,
        identity: Any,
        lg_thread_id: str,
    ) -> dict[str, Any]:
        """Build LangGraph input state compatible with ``MainSupervisorState``.

        The state dict is designed to work with the existing
        ``supervisor_agent.py`` without modifications.

        Crucially, we reset workflow control flags (``status``,
        ``workflow_complete``, ``dialog_state``) so that the supervisor
        treats this as a fresh request even when a previous completed run
        exists on the same LangGraph thread (checkpoint).
        """
        # Check for stored assistant context
        context_key = f"{message.channel_id}:{message.thread_id}"
        assistant_context = self._thread_contexts.get(context_key, {})

        return {
            "messages": [HumanMessage(content=message.text)],
            "user_query": message.text,
            "session_id": lg_thread_id,
            "task_id": f"slack-{message.thread_id}",
            "context": {
                "slack_channel_id": message.channel_id,
                "slack_user_id": message.user_id,
                "slack_thread_ts": message.thread_id,
                "source": "slack",
                "assistant_context": assistant_context,
                "user_identity": {
                    "internal_id": identity.internal_user_id,
                    "display_name": identity.display_name,
                    "rbac_role": identity.rbac_role,
                    "namespaces": identity.allowed_namespaces,
                    "operations": identity.allowed_operations,
                },
            },
            # Reset workflow control so the supervisor treats this as
            # a fresh request, not a continuation of a completed run.
            "status": "pending",
            "workflow_complete": False,
            "dialog_state": [],
        }
