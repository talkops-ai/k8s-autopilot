"""Slack-specific stream output handler.

Implements the :class:`~k8s_autopilot.core.integration.stream_bridge.StreamSink`
protocol using Slack's Chat Streaming APIs:

- ``chat_stream()``     → start a streaming message
- ``streamer.append()`` → append token chunks
- ``streamer.stop()``   → finalize the stream

Also renders HITL ``interrupt()`` payloads as Block Kit approval
buttons and errors as Block Kit error cards.

All markdown output from the agent is converted to Slack mrkdwn
before posting via :func:`md_to_mrkdwn`.
"""

from __future__ import annotations

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient  # type: ignore[import-not-found]

from k8s_autopilot.core.integration.slack.blocks import (
    build_approval_blocks,
    build_error_blocks,
    build_feedback_request_blocks,
    build_planning_approval_blocks,
    build_status_update_blocks,
    build_user_input_blocks,
)
from k8s_autopilot.core.integration.slack.mrkdwn import md_to_mrkdwn
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("SlackStreamSink")


class SlackStreamSink:
    """Implements the ``StreamSink`` protocol for Slack chat streaming.

    This class manages the lifecycle of a single Slack chat stream:
    start → buffer tokens → flush at threshold → stop.

    Token buffering reduces the number of ``chat.appendStream`` API
    calls, improving performance and staying within Slack rate limits.
    The buffer threshold is configurable via
    ``config.SLACK_STREAM_BUFFER_SIZE`` (default 256 characters).

    All text output is converted from standard Markdown to Slack mrkdwn
    format via :func:`md_to_mrkdwn` before being sent.
    """

    def __init__(
        self,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
        config: Any,
    ) -> None:
        self.client = client
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.config = config
        self._streamer: Any = None
        self._buffer: str = ""
        self._buffer_size: int = getattr(config, "SLACK_STREAM_BUFFER_SIZE", 512)
        self._show_thinking: bool = getattr(config, "SLACK_SHOW_THINKING_STEPS", True)
        # Tracks the single live-updating thinking status message
        self._thinking_msg_ts: str | None = None
        # Tracks whether this thread is awaiting a text reply (for chat_continue / feedback)
        self.awaiting_text_reply: bool = False
        # Accumulates all text sent via on_token() so that when an
        # interrupt fires we can include the pre-streamed content
        # (e.g. plan text) in the Block Kit card.
        self._accumulated_text: str = ""
        self.has_streamed: bool = False
        self._stream_started: bool = False

    # ── StreamSink protocol ───────────────────────────────────────────

    async def on_stream_start(
        self,
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Start a Slack chat stream in the specified thread.

        Stops any lingering previous stream first to prevent message
        merging when ``chat_stream()`` is called multiple times in the
        same thread (e.g., after a HITL interrupt → resume cycle).
        """
        # Save channel and thread
        self.channel_id = channel_id
        self.thread_ts = thread_id

        # ── Stop any lingering previous stream ──────────────────────
        if self._streamer is not None:
            logger.info("Stopping lingering previous stream before starting new one")
            try:
                await self._streamer.stop()
            except Exception:
                pass
            self._streamer = None

        # Reset buffers for the new stream
        self._buffer = ""
        self._accumulated_text = ""
        self.has_streamed = False
        self._stream_started = False

    async def on_token(self, token: str) -> None:
        """Buffer a token and flush when the buffer reaches threshold."""
        self._buffer += token
        self._accumulated_text += token
        self.has_streamed = True

        if not self._stream_started:
            self._stream_started = True
            try:
                self._streamer = await self.client.chat_stream(
                    channel=self.channel_id,
                    thread_ts=self.thread_ts,
                )
                logger.info(
                    f"Deferred start of Slack chat stream | channel={self.channel_id} thread={self.thread_ts}"
                )
            except Exception:
                # Fallback: if chat_stream is not available (older SDK),
                # we'll accumulate and post a single message at the end.
                logger.warning(
                    "chat_stream not available — falling back to buffered post"
                )
                self._streamer = None

        if self._streamer and len(self._buffer) >= self._buffer_size:
            await self._flush_buffer()

    async def on_stream_end(self) -> None:
        """Flush any remaining buffer and stop the stream."""
        logger.info(
            f"on_stream_end | has_streamer={self._streamer is not None}"
            f" buffer_len={len(self._buffer)}"
            f" accumulated_len={len(self._accumulated_text)}"
        )
        await self._delete_thinking_msg()
        if self._streamer:
            await self._flush_buffer()
            try:
                await self._streamer.stop()
            except Exception as exc:
                logger.warning(f"Error stopping Slack stream: {exc}")
        elif self._buffer:
            # Fallback: post accumulated content as a single message
            await self.client.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                text=md_to_mrkdwn(self._buffer.strip()),
            )
        self._streamer = None
        self._buffer = ""
        self._accumulated_text = ""

    async def on_pre_interrupt(self) -> None:
        """Capture accumulated text before interrupt handling.

        Flushes remaining buffer so the stream has all text up to
        the interrupt point.  The ``_accumulated_text`` is preserved
        for the upcoming ``on_interrupt()`` call.
        """
        if self._streamer:
            await self._flush_buffer()

    async def on_interrupt(self, interrupt_value: dict[str, Any]) -> None:
        """Route HITL interrupt payloads to the appropriate Block Kit handler.

        Dispatch order:
        1. ``pending_feedback_requests``  → plain text question (awaiting reply)
        2. ``type: "chat_continue"``      → plain text question (awaiting reply)
        3. ``type: "user_input_request"`` → option buttons / planning approval
        4. ``action_requests``            → standard approve/reject card
        5. Fallback: plain ``question``   → plain text question (awaiting reply)

        Uses Slack’s ``streamer.stop(blocks=...)`` to finalize the
        active stream with Block Kit blocks — preserving streamed
        plan text alongside approval buttons in a single message.
        """
        interrupt_type = interrupt_value.get("type", "")
        logger.info(
            f"on_interrupt dispatch | type={interrupt_type!r}"
            f" has_accumulated_text={bool(self._accumulated_text.strip())}"
            f" has_streamer={self._streamer is not None}"
            f" keys={sorted(interrupt_value.keys())}"
        )
        # ── 1. Pending feedback requests (from supervisor classify_request) ──
        if "pending_feedback_requests" in interrupt_value:
            pfr = interrupt_value["pending_feedback_requests"]
            question = pfr.get("question") or interrupt_value.get(
                "question", "I need more information to proceed."
            )
            blocks = build_feedback_request_blocks(md_to_mrkdwn(question))
            await self._stop_stream_with_blocks(blocks, question)
            self.awaiting_text_reply = True
            self._accumulated_text = ""
            return

        # ── 3. User input request (from request_user_input tool) ─────────
        if interrupt_type == "user_input_request":
            # Inject accumulated (pre-streamed) text as plan context
            # if the interrupt payload doesn't already include it.
            context_val = interrupt_value.get("context", "")
            plan_summary_val = interrupt_value.get("plan_summary", "")
            logger.info(
                f"user_input_request | context_len={len(str(context_val))}"
                f" plan_summary_len={len(str(plan_summary_val))}"
                f" accumulated_text_len={len(self._accumulated_text)}"
                f" title={interrupt_value.get('title', '')!r}"
            )

            if self._accumulated_text and not plan_summary_val and not context_val:
                interrupt_value["plan_summary"] = self._accumulated_text.strip()

            options = interrupt_value.get("options", [])
            # Check if this is a planning approval (approve/modify/reject)
            option_keys = {
                str(opt.get("value") or opt.get("key") or "").lower() if isinstance(opt, dict) else str(opt).lower()
                for opt in options
            }
            if option_keys & {"approve", "modify", "reject"}:
                blocks = build_planning_approval_blocks(interrupt_value)
            else:
                blocks = build_user_input_blocks(interrupt_value)
            await self._stop_stream_with_blocks(
                blocks, interrupt_value.get("question", "Input needed")
            )
            self._accumulated_text = ""
            return

        # ── 4. Action requests (from HumanInTheLoopMiddleware) ───────────
        if "action_requests" in interrupt_value:
            blocks = build_approval_blocks(interrupt_value)
            await self._stop_stream_with_blocks(blocks, "Action requires approval")
            self._accumulated_text = ""
            return

        # ── 5. Fallback: plain question field ────────────────────────
        question = interrupt_value.get("question", "")
        if question:
            blocks = build_feedback_request_blocks(md_to_mrkdwn(question))
            await self._stop_stream_with_blocks(blocks, question)
            self.awaiting_text_reply = True
            self._accumulated_text = ""
            return

        # ── Last resort: generic approval ────────────────────────────
        blocks = build_approval_blocks(interrupt_value)
        await self._stop_stream_with_blocks(blocks, "Action requires approval")
        self._accumulated_text = ""

    async def on_error(self, error: Exception) -> None:
        """Post an error Block Kit message.

        Attempts to cleanly stop any active stream before posting
        the error card.
        """
        if self._streamer:
            try:
                await self._streamer.stop()
            except Exception:
                pass

        blocks = build_error_blocks(error)
        await self.client.chat_postMessage(
            channel=self.channel_id,
            thread_ts=self.thread_ts,
            blocks=blocks,
            text=f"Error: {error!s}",
        )

    async def on_subgraph_event(
        self,
        namespace: tuple[str, ...],
        data: dict[str, Any],
    ) -> None:
        """Handle deep-agent / coordinator subgraph activity.

        Posts a context block with the coordinator name and status.
        Enabled via ``config.SLACK_SHOW_THINKING_STEPS`` (default: True).
        """
        if not self._show_thinking:
            return

        # Extract coordinator name from namespace
        # Namespace format: ("node_name:task_id", ...)
        if namespace:
            coordinator = namespace[-1].split(":")[0]
            blocks = build_status_update_blocks(
                title=f"Delegated to {coordinator}",
                status="running",
            )
            try:
                await self.client.chat_postMessage(
                    channel=self.channel_id,
                    thread_ts=self.thread_ts,
                    blocks=blocks,
                    text=f"Delegated to {coordinator}",
                )
            except Exception as exc:
                logger.debug(f"Failed to post subgraph event: {exc}")

    async def on_thinking_step(self, step_text: str) -> None:
        """Show agent activity as a live-updating message under the bot name.

        Posts a single context-block message on the first call, then
        updates it in-place (via ``chat_update``) on subsequent calls.
        This gives an Antigravity-style experience where each step
        replaces the previous one.  The message is deleted when the
        final response arrives (see ``_delete_thinking_msg``).

        Controlled by ``config.SLACK_SHOW_THINKING_STEPS`` (default True).
        """
        if not self._show_thinking:
            return

        status = step_text.strip()
        if not status:
            return

        # Clean up for display (no markdown, single line, max length)
        status = status.replace("**", "").replace("_", "")
        if "\n" in status:
            status = status.split("\n")[0]
        if len(status) > 150:
            status = status[:147] + "…"

        logger.info(f"on_thinking_step | {status[:60]}")

        blocks = [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_{status}_",
                    },
                ],
            },
        ]

        try:
            if self._thinking_msg_ts:
                # Update the existing message in-place
                await self.client.chat_update(
                    channel=self.channel_id,
                    ts=self._thinking_msg_ts,
                    blocks=blocks,
                    text=status,
                )
            else:
                # Post the first thinking message
                resp = await self.client.chat_postMessage(
                    channel=self.channel_id,
                    thread_ts=self.thread_ts,
                    blocks=blocks,
                    text=status,
                )
                self._thinking_msg_ts = resp.get("ts")
        except Exception as exc:
            logger.warning(f"Failed to post/update thinking step: {exc}")

    # ── Internal helpers ──────────────────────────────────────────────

    async def _stop_stream_with_blocks(
        self,
        blocks: list[dict[str, Any]],
        fallback_text: str,
    ) -> None:
        """Stop the active stream and finalize with Block Kit blocks.

        Uses Slack’s ``streamer.stop(blocks=...)`` to append blocks to
        the same message that was streaming — preserving the streamed
        plan text alongside the approval buttons in a single message.

        Falls back to ``chat_postMessage`` if the streamer is
        unavailable or ``stop(blocks=...)`` raises.
        """
        await self._delete_thinking_msg()
        logger.info(
            f"_stop_stream_with_blocks | has_streamer={self._streamer is not None}"
            f" buffer_len={len(self._buffer)} block_count={len(blocks)}"
        )
        if self._streamer:
            await self._flush_buffer()
            try:
                await self._streamer.stop(blocks=blocks)
                self._streamer = None
                return
            except Exception as exc:
                logger.warning(f"streamer.stop(blocks=...) failed: {exc}")
                # Fallback: try plain stop, then post separately
                try:
                    await self._streamer.stop()
                except Exception:
                    pass
                self._streamer = None
        elif self._buffer:
            # No active streamer but we have buffered content — post it
            await self.client.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                text=md_to_mrkdwn(self._buffer.strip()),
            )
            self._buffer = ""

        # Post blocks as a separate message (fallback or no-streamer path)
        await self.client.chat_postMessage(
            channel=self.channel_id,
            thread_ts=self.thread_ts,
            blocks=blocks,
            text=fallback_text,
        )

    async def _flush_buffer(self) -> None:
        """Flush the accumulated token buffer to the Slack stream.

        Sends raw text via ``markdown_text=`` — Slack's streaming API
        handles basic mrkdwn formatting natively.  Full ``md_to_mrkdwn``
        conversion (tables, links, headings) would break when applied to
        partial chunks, so it is deferred to ``on_stream_end`` / ``stop()``.
        """
        if self._buffer and self._streamer:
            try:
                await self._streamer.append(
                    markdown_text=self._buffer,
                )
            except Exception as exc:
                logger.warning(f"Error appending to Slack stream: {exc}")
            self._buffer = ""

    async def _stop_active_stream(self) -> None:
        """Flush buffer, stop any active chat stream, and clean up thinking msg."""
        await self._delete_thinking_msg()
        if self._streamer:
            await self._flush_buffer()
            try:
                await self._streamer.stop()
            except Exception as exc:
                logger.warning(f"Error stopping stream before interrupt: {exc}")
        elif self._buffer:
            await self.client.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                text=md_to_mrkdwn(self._buffer.strip()),
            )
            self._buffer = ""

    async def _delete_thinking_msg(self) -> None:
        """Delete the live-updating thinking message if it exists."""
        if self._thinking_msg_ts:
            try:
                await self.client.chat_delete(
                    channel=self.channel_id,
                    ts=self._thinking_msg_ts,
                )
            except Exception as exc:
                logger.debug(f"Failed to delete thinking msg: {exc}")
            self._thinking_msg_ts = None
