"""Slack Bolt event and action handler registration.

Registers all Slack event listeners and Block Kit action handlers
on the Bolt ``AsyncApp`` instance.  Uses the official **``AsyncAssistant``**
middleware class from Bolt for Python, which is the recommended way to
build agent-style apps in Slack.

The ``AsyncAssistant`` class provides:

- ``@assistant.thread_started`` — new assistant thread opened
- ``@assistant.user_message``  — user message in assistant thread
- ``@assistant.thread_context_changed`` — user navigates channels

Handler categories:

- **Assistant handlers** — via ``AsyncAssistant`` middleware
- **Channel mention** — ``@app_mention`` (channels)
- **HITL actions** — ``approve_*``, ``reject_*`` (Block Kit buttons)

Ref: https://github.com/slackapi/bolt-python/tree/main/examples/assistants
Ref: https://docs.slack.dev/ai/developing-agents
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from slack_bolt.async_app import AsyncApp, AsyncAssistant, AsyncSetStatus, AsyncSay  # type: ignore[import-not-found]

from k8s_autopilot.integrations.base import (
    IncomingMessage,
    InteractionPayload,
)
from k8s_autopilot.integrations.slack.blocks import (
    build_decision_confirmation_blocks,
    build_edit_modal,
)
from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.integrations.slack.integration import (
        SlackMessagingIntegration,
    )

logger = AgentLogger("SlackBoltHandlers")

# Pre-compiled regex for stripping @mention markup from message text
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

# Pre-compiled regex for matching HITL approve/reject action IDs
_APPROVE_RE = re.compile(r"^approve_.*")
_REJECT_RE = re.compile(r"^reject_.*")
_USER_INPUT_OPTION_RE = re.compile(r"^user_input_option_\d+$")


def register_bolt_handlers(
    bolt_app: AsyncApp,
    integration: SlackMessagingIntegration,
) -> None:
    """Register all event and action handlers on the Bolt app.

    Uses the official ``AsyncAssistant`` middleware class for assistant
    container events (thread_started, user_message, thread_context_changed),
    and raw event handlers for channel @mentions and HITL Block Kit actions.

    Args:
        bolt_app: The Slack Bolt ``AsyncApp`` instance.
        integration: The :class:`SlackMessagingIntegration` that
            handles the actual processing.
    """
    # ==================================================================
    # AsyncAssistant middleware (official Bolt pattern for AI agents)
    # ==================================================================

    assistant = AsyncAssistant()

    # ── Thread started ────────────────────────────────────────────────

    @assistant.thread_started
    async def handle_thread_started(
        say: AsyncSay,
    ) -> None:
        """When user opens the assistant container, greet them."""
        await say(":wave: Hi! I'm k8s-autopilot. How can I help you with your Kubernetes clusters today?")

    # ── User message (in assistant thread) ────────────────────────────

    @assistant.user_message
    async def handle_user_message(
        payload: dict[str, Any],
        say: AsyncSay,
        set_status: AsyncSetStatus,
    ) -> None:
        """Handle user messages in the assistant container thread.

        Per Bolt docs, ``AsyncAssistant.user_message`` is the primary
        handler for user messages.  Bolt automatically:
        - Filters out bot messages (no self-reply loops)
        - Only fires for messages in assistant threads
        - Injects ``AsyncSay``, ``AsyncSetStatus``

        Args:
            payload: The event payload with ``text``, ``user``,
                ``channel``, ``thread_ts`` etc.
            say: Bolt's ``AsyncSay`` utility for replying.
            set_status: Bolt's ``AsyncSetStatus`` to show thinking state.
        """
        text = payload.get("text", "").strip()
        if not text:
            return

        # Set thinking status (Bolt handles the 3-second ack)
        await set_status(status="is thinking…")

        message = IncomingMessage(
            text=text,
            user_id=str(payload.get("user", "")),
            thread_id=str(payload.get("thread_ts", payload.get("ts", ""))),
            channel_id=str(payload.get("channel", "")),
            platform="slack",
            raw_event=payload,
        )

        logger.info(
            "Processing assistant message",
            extra={
                "user": message.user_id,
                "channel": message.channel_id,
                "text_preview": text[:50],
            },
        )

        await integration.handle_message(message)

    # ── Thread context changed ────────────────────────────────────────

    @assistant.thread_context_changed
    async def handle_context_changed(
        payload: dict[str, Any],
    ) -> None:
        """When user navigates to a new channel while assistant is open.

        Per Bolt docs, ``AsyncAssistant.thread_context_changed``
        provides the updated context so the agent can be aware of
        the user's current Slack context.
        """
        context = (
            payload.get("assistant_thread", {}).get("context", {})
        )
        channel_id = str(payload.get("channel", ""))
        thread_ts = str(payload.get("thread_ts", payload.get("ts", "")))

        try:
            await integration.update_thread_context(
                channel_id=channel_id,
                thread_ts=thread_ts,
                new_context=context,
            )
            logger.debug(
                "Updated thread context",
                extra={"channel": channel_id, "context": context},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to update thread context: {exc}")

    # Register the assistant middleware with the Bolt app
    bolt_app.use(assistant)

    # ==================================================================
    # Regular DM handler (outside assistant container)
    # ==================================================================
    # AsyncAssistant only handles messages in the assistant side pane.
    # Regular DMs (message.im events) need a separate handler.

    @bolt_app.event("message")
    async def handle_dm_message(
        event: dict[str, Any],
        say: Any,
    ) -> None:
        """Handle DM messages to the bot (outside the assistant container).

        Skips:
        - Messages from bots (to avoid self-reply loops)
        - Message subtypes (edits, deletes, joins, etc.)
        """
        # Guard: skip bot messages and subtypes
        if event.get("bot_id") or event.get("subtype"):
            return

        text = (event.get("text") or "").strip()
        if not text:
            return

        message = IncomingMessage(
            text=text,
            user_id=str(event.get("user", "")),
            thread_id=str(event.get("thread_ts", event.get("ts", ""))),
            channel_id=str(event.get("channel", "")),
            platform="slack",
            raw_event=event,
        )

        logger.info(
            "Processing DM message",
            extra={
                "user": message.user_id,
                "channel": message.channel_id,
                "text_preview": text[:50],
            },
        )

        await integration.handle_message(message)

    # ==================================================================
    # Channel @mention handler (outside assistant container)
    # ==================================================================

    @bolt_app.event("app_mention")
    async def handle_mention(
        event: dict[str, Any],
        say: Any,
        client: Any,
    ) -> None:
        """Handle @mention in channels.

        Strips the ``<@BOT_ID>`` markup from the message text before
        processing.  This is NOT handled by the AsyncAssistant —
        assistant only covers DMs in the assistant container.
        """
        if event.get("bot_id"):
            return

        # Strip @mention markup to get the actual query
        text = _MENTION_RE.sub("", event.get("text", "")).strip()
        if not text:
            return

        message = IncomingMessage(
            text=text,
            user_id=str(event["user"]),
            thread_id=str(event.get("thread_ts", event["ts"])),
            channel_id=str(event["channel"]),
            platform="slack",
            raw_event=event,
        )

        logger.info(
            "Processing @mention",
            extra={
                "user": event["user"],
                "channel": event["channel"],
                "text_preview": text[:50],
            },
        )

        await integration.handle_message(message)

    # ==================================================================
    # HITL Block Kit actions
    # ==================================================================

    async def _update_approval_card(
        client: Any,
        body: dict[str, Any],
        decision: str,
        card_type: str = "tool",
    ) -> None:
        """Replace an approval card with a static decision confirmation.

        Uses ``chat.update`` per Slack docs — requires the full blocks
        list (partial updates are not supported).  Uses
        ``body["container"]["message_ts"]`` as recommended by Bolt SDK
        docs over ``body["message"]["ts"]``.
        """
        try:
            channel_id = body["channel"]["id"]
            message_ts = body.get("container", {}).get(
                "message_ts", body.get("message", {}).get("ts", "")
            )
            user_id = body["user"]["id"]
            original_blocks = body.get("message", {}).get("blocks", [])

            confirmation_blocks = build_decision_confirmation_blocks(
                decision=decision,
                user_id=user_id,
                original_blocks=original_blocks,
                card_type=card_type,
            )

            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=confirmation_blocks,
                text=f"Decision: {decision}",  # Fallback text (required)
            )
        except Exception as exc:
            logger.warning(
                f"Failed to update approval card: {exc}",
                extra={"decision": decision, "card_type": card_type},
            )

    @bolt_app.action(_APPROVE_RE)
    async def handle_approve(ack: Any, body: dict[str, Any], client: Any) -> None:
        """Handle approve button clicks from Block Kit."""
        await ack()

        # Immediate visual feedback — replace buttons with confirmation
        await _update_approval_card(client, body, "approve", card_type="tool")

        action = body["actions"][0]
        msg = body.get("message", {})

        payload = InteractionPayload(
            action_id=action["action_id"],
            user_id=body["user"]["id"],
            thread_id=str(msg.get("thread_ts", msg.get("ts", ""))),
            channel_id=body["channel"]["id"],
            platform="slack",
            value="approve",
            raw_payload=body,
        )

        logger.info(
            "HITL: Approve action",
            extra={
                "action_id": action["action_id"],
                "user": body["user"]["id"],
            },
        )

        await integration.handle_interaction(payload)

    @bolt_app.action(_REJECT_RE)
    async def handle_reject(ack: Any, body: dict[str, Any], client: Any) -> None:
        """Handle reject button clicks from Block Kit."""
        await ack()

        # Immediate visual feedback — replace buttons with confirmation
        await _update_approval_card(client, body, "reject", card_type="tool")

        action = body["actions"][0]
        msg = body.get("message", {})

        payload = InteractionPayload(
            action_id=action["action_id"],
            user_id=body["user"]["id"],
            thread_id=str(msg.get("thread_ts", msg.get("ts", ""))),
            channel_id=body["channel"]["id"],
            platform="slack",
            value="reject",
            raw_payload=body,
        )

        logger.info(
            "HITL: Reject action",
            extra={
                "action_id": action["action_id"],
                "user": body["user"]["id"],
            },
        )

        await integration.handle_interaction(payload)

    # ── Retry / Escalate actions ──────────────────────────────────────

    @bolt_app.action("retry_last")
    async def handle_retry(ack: Any, body: dict[str, Any], client: Any) -> None:
        """Handle retry button clicks from error cards."""
        await ack()
        # TODO: implement retry logic — re-run the last message
        logger.info("Retry requested", extra={"user": body["user"]["id"]})

    @bolt_app.action("escalate_to_human")
    async def handle_escalate(
        ack: Any, body: dict[str, Any], client: Any,
    ) -> None:
        """Handle escalate button clicks from error cards."""
        await ack()
        # TODO: implement escalation — notify ops channel
        logger.info(
            "Escalation requested", extra={"user": body["user"]["id"]},
        )

    # ==================================================================
    # Planning approval actions (3-button: Approve / Edit / Reject)
    # ==================================================================

    @bolt_app.action("approve_plan")
    async def handle_approve_plan(
        ack: Any, body: dict[str, Any], client: Any,
    ) -> None:
        """Handle plan approval from planning review card."""
        await ack()

        # Immediate visual feedback — replace buttons with confirmation
        await _update_approval_card(client, body, "approve", card_type="plan")

        msg = body.get("message", {})
        payload = InteractionPayload(
            action_id="approve_plan",
            user_id=body["user"]["id"],
            thread_id=str(msg.get("thread_ts", msg.get("ts", ""))),
            channel_id=body["channel"]["id"],
            platform="slack",
            value="approve",
            raw_payload=body,
        )

        logger.info(
            "HITL: Plan approved",
            extra={"user": body["user"]["id"]},
        )
        await integration.handle_interaction(payload)

    @bolt_app.action("edit_plan")
    async def handle_edit_plan(
        ack: Any, body: dict[str, Any], client: Any,
    ) -> None:
        """Handle 'Edit' button — opens a Slack modal for text input."""
        await ack()

        # Immediate visual feedback — replace buttons with edit confirmation
        await _update_approval_card(client, body, "edit", card_type="plan")

        msg = body.get("message", {})
        thread_ts = str(msg.get("thread_ts", msg.get("ts", "")))
        channel_id = body["channel"]["id"]
        trigger_id = body["trigger_id"]

        modal = build_edit_modal(
            thread_ts=thread_ts,
            channel_id=channel_id,
        )

        await client.views_open(
            trigger_id=trigger_id,
            view=modal,
        )

        logger.info(
            "HITL: Edit modal opened",
            extra={"user": body["user"]["id"], "thread": thread_ts},
        )

    @bolt_app.action("reject_plan")
    async def handle_reject_plan(
        ack: Any, body: dict[str, Any], client: Any,
    ) -> None:
        """Handle plan rejection from planning review card."""
        await ack()

        # Immediate visual feedback — replace buttons with confirmation
        await _update_approval_card(client, body, "reject", card_type="plan")

        msg = body.get("message", {})
        payload = InteractionPayload(
            action_id="reject_plan",
            user_id=body["user"]["id"],
            thread_id=str(msg.get("thread_ts", msg.get("ts", ""))),
            channel_id=body["channel"]["id"],
            platform="slack",
            value="reject",
            raw_payload=body,
        )

        logger.info(
            "HITL: Plan rejected",
            extra={"user": body["user"]["id"]},
        )
        await integration.handle_interaction(payload)

    # ==================================================================
    # Edit plan modal submission
    # ==================================================================

    @bolt_app.view("edit_plan_modal")
    async def handle_edit_modal_submission(
        ack: Any, body: dict[str, Any], client: Any,
    ) -> None:
        """Handle modal submission with edited plan text.

        Extracts the user's modifications and resumes the graph
        with ``{decision: "modify", message: "..."}``.
        """
        await ack()

        # Extract edited text from modal
        values = body.get("view", {}).get("state", {}).get("values", {})
        edit_text = (
            values
            .get("edit_input_block", {})
            .get("edit_text", {})
            .get("value", "")
        )

        # Extract thread context from private_metadata
        private_metadata = body.get("view", {}).get("private_metadata", "")
        parts = private_metadata.split(":", 1)
        if len(parts) == 2:
            channel_id, thread_ts = parts
        else:
            logger.warning(f"Invalid private_metadata in edit modal: {private_metadata}")
            return

        payload = InteractionPayload(
            action_id="edit_plan",
            user_id=body["user"]["id"],
            thread_id=thread_ts,
            channel_id=channel_id,
            platform="slack",
            value="edit",
            raw_payload={"edit_text": edit_text, **body},
        )

        logger.info(
            "HITL: Plan edit submitted",
            extra={
                "user": body["user"]["id"],
                "edit_preview": edit_text[:50],
            },
        )

        await integration.handle_interaction(payload)

    # ==================================================================
    # User input option buttons
    # ==================================================================

    @bolt_app.action(_USER_INPUT_OPTION_RE)
    async def handle_user_input_option(
        ack: Any, body: dict[str, Any], client: Any,
    ) -> None:
        """Handle user input option button clicks.

        These are dynamic buttons generated by ``build_user_input_blocks()``
        for user input requests.
        """
        await ack()

        action = body["actions"][0]
        msg = body.get("message", {})

        payload = InteractionPayload(
            action_id=action["action_id"],
            user_id=body["user"]["id"],
            thread_id=str(msg.get("thread_ts", msg.get("ts", ""))),
            channel_id=body["channel"]["id"],
            platform="slack",
            value=action.get("value", ""),
            raw_payload=body,
        )

        logger.info(
            "HITL: User input option selected",
            extra={
                "action_id": action["action_id"],
                "value": action.get("value"),
                "user": body["user"]["id"],
            },
        )

        await integration.handle_interaction(payload)
