"""Block Kit payload builders for Slack integration.

All builders return ``list[dict]`` compatible with Slack's ``blocks``
parameter on ``chat.postMessage`` and similar API methods.

These builders are pure functions with no side-effects — they only
construct JSON payloads.  The actual posting is handled by the
:class:`~k8s_autopilot.integrations.slack.stream_sink.SlackStreamSink`.

Text fields that accept ``mrkdwn`` type receive pre-converted content
from :func:`~k8s_autopilot.integrations.slack.mrkdwn.md_to_mrkdwn`.
"""

from __future__ import annotations

import json
from typing import Any

from k8s_autopilot.integrations.slack.mrkdwn import md_to_mrkdwn

# ---------------------------------------------------------------------------
# HITL approval blocks (Approve / Reject — 2-button)
# ---------------------------------------------------------------------------


def build_approval_blocks(
    interrupt_value: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build Approve / Reject buttons for HITL tool-execution gates.

    Visually distinguished from plan-review cards by using the ⚙️ gear
    header and a context footer identifying the tool.

    Args:
        interrupt_value: The payload from ``interrupt({...})`` in the
            graph node.  Expected keys: ``question``, ``details``,
            ``action`` (all optional with sensible defaults).

    Returns:
        Slack Block Kit blocks with an actions section.
    """
    question = str(
        interrupt_value.get(
            "question",
            interrupt_value.get("message", "Action requires approval"),
        )
        or "Action requires approval"
    )
    details = interrupt_value.get(
        "details",
        interrupt_value.get("action_description", ""),
    )
    action_name = interrupt_value.get("action", "action")

    # ── Action requests (from LangChain HITL middleware) ──────────────
    action_requests = interrupt_value.get("action_requests", [])
    tool_names: list[str] = []
    if action_requests and not details:
        parts = []
        for req in action_requests:
            name = req.get("name", "unknown")
            tool_names.append(name)
            args = req.get("arguments", {})
            desc = req.get("description", "")
            args_str = json.dumps(args, indent=2) if isinstance(args, dict) else str(args)

            if desc:
                parts.append(f"**Tool:** `{name}`\n{desc}\n**Args:**\n```\n{args_str}\n```")
            else:
                parts.append(f"**Tool:** `{name}`\n**Args:**\n```\n{args_str}\n```")
        details = "\n\n".join(parts)
    elif action_requests:
        tool_names = [req.get("name", "unknown") for req in action_requests]

    # ── Build blocks ──────────────────────────────────────────────────
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":gear: *Tool Execution Approval*\n{md_to_mrkdwn(question)}",
            },
        },
    ]

    if details:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": md_to_mrkdwn(str(details)[:2900]),
                },
            },
        )

    # Divider before action buttons
    blocks.append({"type": "divider"})

    blocks.append(
        {
            "type": "actions",
            "block_id": f"approval_{action_name}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":white_check_mark: Approve"},
                    "style": "primary",
                    "action_id": f"approve_{action_name}",
                    "value": action_name,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":x: Reject"},
                    "style": "danger",
                    "action_id": f"reject_{action_name}",
                    "value": action_name,
                },
            ],
        },
    )

    # Context footer showing the tool name(s)
    tool_label = ", ".join(f"`{t}`" for t in tool_names) if tool_names else f"`{action_name}`"
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":lock: Tool {tool_label} requires approval before execution",
                },
            ],
        },
    )

    return blocks


# ---------------------------------------------------------------------------
# Planning approval blocks (Approve / Edit / Reject — 3-button)
# ---------------------------------------------------------------------------


def build_planning_approval_blocks(
    interrupt_value: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build Approve / Edit / Reject buttons for planning review.

    Used when the agent presents a plan (e.g. deployment plan,
    migration plan) and needs approval, modification, or rejection.

    The "Edit" button triggers a Slack modal where the user can
    provide modified instructions.

    Args:
        interrupt_value: Dict with ``question``, ``context`` or
            ``details``, and optional ``plan_summary``.

    Returns:
        Slack Block Kit blocks with a 3-button action section.
    """
    question = interrupt_value.get("question", "Please review the following plan:")
    context_text = interrupt_value.get("context", interrupt_value.get("details", ""))
    plan_summary = interrupt_value.get("plan_summary", "")

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":clipboard: *Plan Review*\n{md_to_mrkdwn(question)}",
            },
        },
    ]

    if plan_summary:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{plan_summary[:2900]}```",
                },
            },
        )
    elif context_text:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": md_to_mrkdwn(str(context_text)[:2900]),
                },
            },
        )

    # Divider before action buttons
    blocks.append({"type": "divider"})

    blocks.append(
        {
            "type": "actions",
            "block_id": "planning_approval",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":white_check_mark: Approve"},
                    "style": "primary",
                    "action_id": "approve_plan",
                    "value": "approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":pencil2: Edit"},
                    "action_id": "edit_plan",
                    "value": "edit",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":x: Reject"},
                    "style": "danger",
                    "action_id": "reject_plan",
                    "value": "reject",
                },
            ],
        },
    )

    # Context footer
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":mag: Review the plan before proceeding",
                },
            ],
        },
    )

    return blocks


# ---------------------------------------------------------------------------
# Post-click decision confirmation blocks
# ---------------------------------------------------------------------------


_DECISION_EMOJI: dict[str, str] = {
    "approve": ":white_check_mark:",
    "reject": ":x:",
    "edit": ":pencil2:",
}

_DECISION_LABEL: dict[str, str] = {
    "approve": "Approved",
    "reject": "Rejected",
    "edit": "Modifications Requested",
}


def build_decision_confirmation_blocks(
    *,
    decision: str,
    user_id: str,
    original_blocks: list[dict[str, Any]] | None = None,
    card_type: str = "tool",
) -> list[dict[str, Any]]:
    """Build read-only confirmation blocks that replace an approval card.

    Called via ``chat.update`` after the user clicks Approve / Reject / Edit
    to provide immediate visual feedback.  The interactive buttons are
    removed and replaced with a static confirmation showing the decision.

    Per Slack docs, ``chat.update`` requires the full ``blocks`` list
    (partial updates are not supported).

    Args:
        decision: One of ``"approve"``, ``"reject"``, ``"edit"``.
        user_id: Slack user ID of the person who acted.
        original_blocks: The original message blocks.  Content sections
            (non-action, non-context) are preserved as read-only context.
        card_type: ``"plan"`` for plan review, ``"tool"`` for tool approval.

    Returns:
        Slack Block Kit blocks with the decision confirmation.
    """
    emoji = _DECISION_EMOJI.get(decision, ":grey_question:")
    label = _DECISION_LABEL.get(decision, decision.title())
    header_emoji = ":clipboard:" if card_type == "plan" else ":gear:"
    header_text = "Plan Review" if card_type == "plan" else "Tool Execution Approval"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{header_emoji} *{header_text}*",
            },
        },
    ]

    # Preserve content sections from the original card (skip actions,
    # context footers, dividers, and the original header).
    if original_blocks:
        for i, block in enumerate(original_blocks):
            btype = block.get("type", "")
            if btype in ("actions", "divider", "context"):
                continue
            # Skip the first section (header) — we replaced it above
            if i == 0 and btype == "section":
                continue
            blocks.append(block)

    blocks.append({"type": "divider"})

    # Decision confirmation
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{label}* by <@{user_id}>",
            },
        },
    )

    return blocks


# ---------------------------------------------------------------------------
# User input blocks (dynamic option buttons)
# ---------------------------------------------------------------------------


def build_user_input_blocks(
    interrupt_value: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build option buttons for user input requests.

    Used when the agent needs user selection from a set of choices
    (e.g. selecting a namespace, choosing a strategy).

    Args:
        interrupt_value: Dict with ``question`` and ``options`` (list of
            dicts with ``label`` and ``value`` keys, or plain strings).

    Returns:
        Slack Block Kit blocks with dynamic button options.
    """
    question = interrupt_value.get("question", "Please select an option:")
    options = interrupt_value.get("options", [])

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": md_to_mrkdwn(question),
            },
        },
    ]

    # Build option buttons
    button_elements: list[dict[str, Any]] = []
    for i, opt in enumerate(options[:5]):  # Slack limits to 25, keep it reasonable
        if isinstance(opt, dict):
            label = str(opt.get("label") or opt.get("title") or f"Option {i + 1}")
            value = str(opt.get("value") or opt.get("key") or label)
        else:
            label = str(opt)
            value = str(opt)

        button_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label[:75]},
                "action_id": f"user_input_option_{i}",
                "value": value,
            },
        )

    if button_elements:
        blocks.append(
            {
                "type": "actions",
                "block_id": "user_input_options",
                "elements": button_elements,
            },
        )

    # If there are input fields, add a hint to type a reply
    input_fields = interrupt_value.get("input_fields", [])
    if input_fields:
        field_names = ", ".join(f.get("label", f.get("name", "field")) for f in input_fields)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_Or reply with text to provide: {field_names}_",
                    },
                ],
            },
        )

    return blocks


# ---------------------------------------------------------------------------
# Feedback request blocks (conversational text reply)
# ---------------------------------------------------------------------------


def build_feedback_request_blocks(
    question_mrkdwn: str,
) -> list[dict[str, Any]]:
    """Build a question block that expects a text reply.

    Used for ``pending_feedback_requests``, ``chat_continue``, and
    plain ``question``-only interrupts. The user responds by typing
    a message in the thread — no buttons needed.

    Args:
        question_mrkdwn: The question text, already converted to mrkdwn.

    Returns:
        Slack Block Kit blocks with the question and a reply hint.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": question_mrkdwn,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Reply in this thread to continue_",
                },
            ],
        },
    ]
    return blocks


# ---------------------------------------------------------------------------
# Edit modal builder (for planning approval "Edit" flow)
# ---------------------------------------------------------------------------


def build_edit_modal(
    *,
    thread_ts: str,
    channel_id: str,
    original_plan: str = "",
) -> dict[str, Any]:
    """Build a Slack modal view for editing a plan.

    Opened when the user clicks "Edit" on a planning approval card.
    The user can modify the plan text and submit.

    Args:
        thread_ts: The thread timestamp to associate the edit with.
        channel_id: The channel where the edit modal was triggered.
        original_plan: Optional pre-filled plan text.

    Returns:
        Slack modal view payload for ``views.open``.
    """
    return {
        "type": "modal",
        "callback_id": "edit_plan_modal",
        "title": {"type": "plain_text", "text": "Edit Plan"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": f"{channel_id}:{thread_ts}",
        "blocks": [
            {
                "type": "input",
                "block_id": "edit_input_block",
                "label": {
                    "type": "plain_text",
                    "text": "Your modifications or instructions",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_text",
                    "multiline": True,
                    "initial_value": original_plan,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Describe what changes you'd like...",
                    },
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# Error blocks
# ---------------------------------------------------------------------------


def build_error_blocks(
    error: Exception,
    *,
    include_retry: bool = True,
    include_escalate: bool = True,
) -> list[dict[str, Any]]:
    """Build error notification blocks.

    Args:
        error: The exception that occurred.
        include_retry: Whether to include a "Retry" button.
        include_escalate: Whether to include an "Escalate" button.

    Returns:
        Slack Block Kit blocks with error details and optional actions.
    """
    error_text = str(error)[:500]  # Truncate long error messages
    error_type = type(error).__name__

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f":rotating_light: *Error*\n`{error_type}`: {error_text}"),
            },
        },
    ]

    actions: list[dict[str, Any]] = []
    if include_retry:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Retry"},
                "action_id": "retry_last",
            },
        )
    if include_escalate:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":telephone_receiver: Escalate"},
                "action_id": "escalate_to_human",
            },
        )

    if actions:
        blocks.append(
            {
                "type": "actions",
                "block_id": "error_actions",
                "elements": actions,
            },
        )

    return blocks


# ---------------------------------------------------------------------------
# Task plan / timeline blocks
# ---------------------------------------------------------------------------


_STATUS_EMOJI: dict[str, str] = {
    "pending": ":white_large_square:",
    "running": ":arrows_counterclockwise:",
    "done": ":white_check_mark:",
    "failed": ":x:",
    "skipped": ":fast_forward:",
}


def build_task_plan_blocks(
    task_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build task plan card blocks for multi-step workflows.

    Args:
        task_plan: Dict with ``title`` and ``steps`` (list of dicts
            with ``description`` and ``status`` keys).

    Returns:
        Slack Block Kit blocks showing a step-by-step plan.
    """
    title = task_plan.get("title", "Task Plan")
    steps = task_plan.get("steps", [])

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":clipboard: {title}",
            },
        },
    ]

    for i, step in enumerate(steps, 1):
        status = step.get("status", "pending")
        emoji = _STATUS_EMOJI.get(status, ":white_large_square:")
        description = step.get("description", "")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *Step {i}:* {description}",
                },
            },
        )

    return blocks


# ---------------------------------------------------------------------------
# Status update blocks (thinking steps)
# ---------------------------------------------------------------------------


def build_status_update_blocks(
    title: str,
    status: str,
    details: str | None = None,
) -> list[dict[str, Any]]:
    """Build a status update block (for coordinator activity).

    Args:
        title: Short title for the status update.
        status: Current status (``"running"``, ``"done"``, ``"failed"``).
        details: Optional additional details.

    Returns:
        Slack Block Kit blocks for a status update.
    """
    emoji = _STATUS_EMOJI.get(status, ":information_source:")

    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{emoji} *{title}* — _{status}_",
                },
            ],
        },
    ]

    if details:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": details[:300]},
                ],
            },
        )

    return blocks
