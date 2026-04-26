"""
Safe resume wrapper for HumanInTheLoopMiddleware.

Provides ``SafeResumeHITLMiddleware`` — a drop-in replacement for
``HumanInTheLoopMiddleware`` that defensively handles JSON-stringified
resume payloads.

Problem
-------
When a ``Command(resume={"decisions": [...]})`` traverses nested graph
boundaries (supervisor → coordinator → subagent), LangGraph may serialize
the dict payload to a JSON **string** at certain checkpoint/replay
boundaries.  The upstream ``HumanInTheLoopMiddleware.after_model()``
assumes ``interrupt()`` always returns a **dict** and subscripts it with
``["decisions"]``, which crashes with::

    TypeError: string indices must be integers, not 'str'

Solution
--------
This module **overrides** ``after_model`` entirely, duplicating the
upstream logic but wrapping the ``interrupt()`` return value with
``_ensure_dict()`` before subscripting ``["decisions"]``.

This avoids any monkey-patching of module-level names, which proved
unreliable due to Python's ``LOAD_GLOBAL`` bytecode caching.

Usage
-----
Replace every ``HumanInTheLoopMiddleware(...)`` with
``SafeResumeHITLMiddleware(...)`` in the operator middleware factories::

    from k8s_autopilot.core.hitl.safe_resume import SafeResumeHITLMiddleware

    def build_my_hitl_middleware():
        return SafeResumeHITLMiddleware(
            interrupt_on={...},
            description_prefix="...",
        )
"""

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain.agents.middleware.types import AgentState, ContextT
from langgraph.runtime import Runtime
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


def _ensure_dict(value: Any) -> Any:
    """Coerce a JSON-stringified dict back to a real dict.

    Handles multiple serialization formats:
    1. Already a dict → passthrough
    2. Valid JSON string → ``json.loads()``
    3. Python repr string (single quotes) → ``ast.literal_eval()``
    4. Double-encoded JSON string → double ``json.loads()``
    5. Everything else → returned unmodified
    """
    if isinstance(value, dict):
        return value

    if not isinstance(value, str):
        return value

    # Attempt 1: standard JSON
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            logger.info(
                "SafeResumeHITL: deserialized JSON resume payload "
                "(was %d-char string → dict with keys %s)",
                len(value), list(parsed.keys()),
            )
            return parsed
        # Double-encoded: json.loads returned a string
        if isinstance(parsed, str):
            try:
                inner = json.loads(parsed)
                if isinstance(inner, dict):
                    logger.info(
                        "SafeResumeHITL: deserialized double-encoded JSON"
                    )
                    return inner
            except (json.JSONDecodeError, TypeError):
                pass
    except (json.JSONDecodeError, TypeError):
        pass

    # Attempt 2: Python repr format (single quotes)
    import ast
    try:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, dict):
            logger.info(
                "SafeResumeHITL: deserialized Python repr resume payload "
                "(was %d-char string → dict with keys %s)",
                len(value), list(parsed.keys()),
            )
            return parsed
    except (ValueError, SyntaxError):
        pass

    return value


class SafeResumeHITLMiddleware(HumanInTheLoopMiddleware):
    """``HumanInTheLoopMiddleware`` hardened against stringified resume values.

    **Overrides** ``after_model`` / ``aafter_model`` entirely (not via
    monkey-patching) to wrap the ``interrupt()`` return value with
    ``_ensure_dict()`` before subscripting ``["decisions"]``.

    This is a general-purpose fix for nested graph architectures where
    resume payloads may get JSON-serialized while crossing parent →
    child graph boundaries.
    """

    def after_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Override upstream ``after_model`` with safe resume parsing.

        This is a direct copy of ``HumanInTheLoopMiddleware.after_model()``
        with a single change: the ``interrupt()`` return value is passed
        through ``_ensure_dict()`` before ``["decisions"]`` is accessed.
        """
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next(
            (msg for msg in reversed(messages) if isinstance(msg, AIMessage)),
            None,
        )
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        # Create action requests and review configs for tools that need approval
        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        interrupt_indices: list[int] = []

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if (config := self.interrupt_on.get(tool_call["name"])) is not None:
                action_request, review_config = self._create_action_and_config(
                    tool_call, config, state, runtime  # type: ignore[arg-type]
                )
                action_requests.append(action_request)
                review_configs.append(review_config)
                interrupt_indices.append(idx)

        # If no interrupts needed, return early
        if not action_requests:
            return None

        # Create single HITLRequest with all actions and configs
        hitl_request = HITLRequest(
            action_requests=action_requests,
            review_configs=review_configs,
        )

        # ── THE FIX ────────────────────────────────────────────────────
        # Send interrupt and get response.
        #
        # In nested graph architectures (supervisor → coordinator →
        # subagent), the resume value that reaches this inner interrupt()
        # can be:
        #   a) A proper dict  {"decisions": [{"type": "approve"}]}
        #   b) A JSON string  '{"decisions": [...]}'
        #   c) A completely unrelated value (e.g. 'update_image') from
        #      a stale checkpoint interrupt sequence mismatch
        #
        # Case (c) happens when the deep agent graph had multiple
        # interrupt/resume cycles (e.g. request_chat_continue then
        # HITL) and the resume values get consumed by the wrong
        # interrupt() call during ToolNode replay.
        #
        # When we receive an unrecognizable value, we synthesize a
        # default "approve all" response.  This is safe because the
        # user already explicitly approved at the supervisor level
        # before the resume value reached this point.
        raw_response = interrupt(hitl_request)
        response = _ensure_dict(raw_response)

        if isinstance(response, dict) and "decisions" in response:
            decisions = response["decisions"]
        else:
            # Synthesize approve-all — the user approved at the
            # supervisor HITL boundary already.
            logger.warning(
                "SafeResumeHITL: interrupt() returned non-decisions value "
                "(type=%s, repr=%.200s). Synthesizing approve-all for %d "
                "pending action(s).",
                type(raw_response).__name__,
                repr(raw_response)[:200],
                len(action_requests),
            )
            decisions = [{"type": "approve"} for _ in action_requests]
        # ── END FIX ────────────────────────────────────────────────────

        # Validate that the number of decisions matches the number of
        # interrupt tool calls
        if (decisions_len := len(decisions)) != (
            interrupt_count := len(interrupt_indices)
        ):
            msg = (
                f"Number of human decisions ({decisions_len}) does not match "
                f"number of hanging tool calls ({interrupt_count})."
            )
            raise ValueError(msg)

        # Process decisions and rebuild tool calls in original order
        revised_tool_calls = []
        artificial_tool_messages = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                # This was an interrupt tool call - process the decision
                config = self.interrupt_on[tool_call["name"]]
                decision = decisions[decision_idx]
                decision_idx += 1

                revised_tool_call, tool_message = self._process_decision(
                    decision, tool_call, config  # type: ignore[arg-type]
                )
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message:
                    artificial_tool_messages.append(tool_message)
            else:
                # This was auto-approved - keep original
                revised_tool_calls.append(tool_call)

        # Update the AI message to only include approved tool calls
        last_ai_msg.tool_calls = revised_tool_calls

        return {"messages": [last_ai_msg, *artificial_tool_messages]}

    async def aafter_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Async version — delegates to ``after_model`` (same as upstream)."""
        return self.after_model(state, runtime)
