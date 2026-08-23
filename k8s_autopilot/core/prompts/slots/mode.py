"""
Interaction mode slot resolvers.

Provides slot resolvers for interactive vs headless prompt behavior:
  - ``mode_description`` — how the agent is running
  - ``interactive_preamble`` — context about the execution environment
  - ``ambiguity_guidance`` — ask vs assume behavior
  - ``todo_guidance`` — todo list management rules per mode

Directly adapted from dcode agent.py lines 832–883.
"""

from __future__ import annotations

from k8s_autopilot.core.prompts.resolver import (
    InteractionMode,
    PromptContext,
    PromptSlot,
)


def _resolve_mode_description(ctx: PromptContext) -> str:
    """Resolve ``{mode_description}`` — how the agent is running."""
    if ctx.mode == InteractionMode.INTERACTIVE:
        return "an interactive A2A session on the user's infrastructure"
    return (
        "non-interactive (headless) mode — there is no human operator "
        "monitoring your output in real time"
    )


def _resolve_interactive_preamble(ctx: PromptContext) -> str:
    """Resolve ``{interactive_preamble}`` — mode-specific preamble."""
    if ctx.mode == InteractionMode.INTERACTIVE:
        return (
            "The user sends you messages and you respond with text and tool "
            "calls. Your tools run on the user's infrastructure. The user can "
            "see your responses and tool outputs in real time, so keep them "
            "informed — but don't over-explain."
        )
    return (
        "You received a single task and must complete it fully and "
        "autonomously. There is no human available to answer follow-up "
        "questions, so do NOT ask for clarification — make reasonable "
        "assumptions and proceed."
    )


def _resolve_ambiguity_guidance(ctx: PromptContext) -> str:
    """Resolve ``{ambiguity_guidance}`` — ask vs assume behavior."""
    if ctx.mode == InteractionMode.INTERACTIVE:
        return (
            "- If the request is ambiguous, ask questions before acting.\n"
            "- If asked how to approach something, explain first, then act."
        )
    return (
        "- Do NOT ask clarifying questions — there is no human to answer "
        "them. Make reasonable assumptions and proceed.\n"
        "- If you encounter ambiguity, choose the most reasonable "
        "interpretation and note your assumption briefly.\n"
        "- Always use non-interactive command variants — no human is "
        "available to respond to prompts. Examples: `npm init -y` not "
        "`npm init`, `apt-get install -y` not `apt-get install`, "
        "`yes |` or `--no-input`/`--non-interactive` flags where "
        "available. Never run commands that block waiting for stdin."
    )


def _resolve_todo_guidance(ctx: PromptContext) -> str:
    """Resolve ``{todo_guidance}`` — todo management rules per mode."""
    if ctx.mode == InteractionMode.INTERACTIVE:
        return (
            "6. When first creating a todo list for a task, ALWAYS ask the user if "
            "the plan looks good before starting work\n"
            '   - Create the todos, then ask: "Does this plan '
            'look good?" or similar\n'
            "   - Wait for the user's response before marking the first todo as "
            "in_progress\n"
            "7. Update todo status promptly as you complete each item"
        )
    return (
        "6. There is no human operator in this mode — do NOT ask the user to "
        "approve your plan or wait for a reply.\n"
        "   After you create todos for a multi-step task, mark the first item "
        "`in_progress` immediately and start work.\n"
        "   If the plan needs adjustment, revise the todo list yourself; do "
        "not block on human confirmation.\n"
        "7. Update todo status promptly as you complete each item"
    )


def get_mode_slots() -> list[PromptSlot]:
    """Return all interaction-mode slot resolvers."""
    return [
        PromptSlot(name="mode_description", resolver=_resolve_mode_description),
        PromptSlot(name="interactive_preamble", resolver=_resolve_interactive_preamble),
        PromptSlot(name="ambiguity_guidance", resolver=_resolve_ambiguity_guidance),
        PromptSlot(name="todo_guidance", resolver=_resolve_todo_guidance),
    ]
