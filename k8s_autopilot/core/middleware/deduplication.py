"""Duplicate tool call guard middleware — prevents identical repeat calls."""

import hashlib
import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolCall
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("DuplicateGuard")


@register_middleware(name="duplicate_tool_call_guard")
class DuplicateToolCallGuardMiddleware(BaseAgentMiddleware):
    """Deduplicates identical tool calls within a single agent invocation.

    Hashes ``(tool_name, serialized_args)`` for each tool call.  If a
    duplicate is detected, returns the cached result as a ``ToolMessage``
    instead of re-executing the tool.
    """

    def __init__(self, *, max_duplicates: int = 1, exempt_tools: list[str] | None = None) -> None:
        super().__init__()
        self._seen: dict[str, ToolMessage] = {}
        self._call_counts: dict[str, int] = {}
        self._max_duplicates = max_duplicates
        self._exempt_tools = exempt_tools or [
            "build_obs_a2ui",
            "build_obs_dashboard",
        ]

    def _make_key(self, tool_call: ToolCall) -> str:
        """Hash (tool_name, sorted_args) into a compact key."""
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        args_str = json.dumps(args, sort_keys=True, default=str)
        return hashlib.md5(f"{name}:{args_str}".encode()).hexdigest()

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept each tool call; return cached result for duplicates.

        This method uses the official LangChain ``wrap_tool_call`` hook.
        The handler is called zero times (short-circuit / cache hit) or
        once (normal flow).
        """
        tool_call = request.tool_call
        key = self._make_key(tool_call)
        tool_name = tool_call.get("name", "unknown")

        if tool_name in self._exempt_tools:
            return handler(request)

        # Track call count for this (tool, args) pair
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        count = self._call_counts[key]

        if count > self._max_duplicates and key in self._seen:
            logger.info(
                f"Blocked duplicate call #{count} to '{tool_name}' — "
                f"returning cached result (max_duplicates={self._max_duplicates})"
            )
            cached = self._seen[key]
            # Return a new ToolMessage with the correct tool_call_id so
            # LangGraph can match it to the pending tool_use block.
            return ToolMessage(
                content=(
                    f"[DUPLICATE CALL BLOCKED — identical call was already made "
                    f"and returned the same result. Use the previous result.]\n\n"
                    f"{cached.content}"
                ),
                tool_call_id=tool_call.get("id", ""),
            )

        # First call (or within max_duplicates) — execute normally
        result = handler(request)

        # Cache the result if it's a ToolMessage (not a Command)
        if isinstance(result, ToolMessage):
            self._seen[key] = result

        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable,
    ) -> ToolMessage | Command:
        """Async version — same dedup logic, async handler."""
        tool_call = request.tool_call
        key = self._make_key(tool_call)
        tool_name = tool_call.get("name", "unknown")

        if tool_name in self._exempt_tools:
            return await handler(request)

        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        count = self._call_counts[key]

        if count > self._max_duplicates and key in self._seen:
            logger.info(
                f"Blocked duplicate call #{count} to '{tool_name}' — "
                f"returning cached result (max_duplicates={self._max_duplicates})"
            )
            cached = self._seen[key]
            return ToolMessage(
                content=(
                    f"[DUPLICATE CALL BLOCKED — identical call was already made "
                    f"and returned the same result. Use the previous result.]\n\n"
                    f"{cached.content}"
                ),
                tool_call_id=tool_call.get("id", ""),
            )

        result = await handler(request)

        if isinstance(result, ToolMessage):
            self._seen[key] = result

        return result
