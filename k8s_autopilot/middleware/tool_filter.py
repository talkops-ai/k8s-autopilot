"""Tool filtering proxy middleware for restricting subagent tool access."""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, Callable, Sequence
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from k8s_autopilot.middleware.registry import register_middleware

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


TOOL_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "read": ("read_file", "view_file", "read_url_content"),
    "write": ("write_to_file", "write_file"),
    "edit": ("replace_file_content", "multi_replace_file_content", "edit_file"),
    "grep": ("grep_search", "grep"),
    "glob": ("glob", "file_search", "dir_list"),
    "ls": ("ls", "list_dir", "dir_list"),
    "list_dir": ("ls", "list_dir", "dir_list"),
    "bash": ("run_command", "execute"),
    "execute": ("run_command", "execute"),
}


def _expand_tool_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    for pattern in patterns:
        expanded.append(pattern)
        clean = pattern.strip().lower()
        if clean in TOOL_ALIAS_MAP:
            expanded.extend(TOOL_ALIAS_MAP[clean])
    return tuple(expanded)


@register_middleware(name="tool_filter")
class ToolFilterMiddleware(AgentMiddleware[Any, Any]):
    """Filters tool calls against a whitelist of allowed tool patterns (fnmatch format)."""

    def __init__(self, allowed_patterns: Sequence[str] | None = None) -> None:
        super().__init__()
        self._allowed_patterns = (
            _expand_tool_patterns(allowed_patterns) if allowed_patterns is not None else ()
        )

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool name matches any of the allowed patterns."""
        if not self._allowed_patterns:
            return True
        for pattern in self._allowed_patterns:
            if fnmatch.fnmatch(tool_name, pattern) or fnmatch.fnmatch(tool_name.lower(), pattern.lower()):
                return True
        return False

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = request.tool_call.get("name", "") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else ""
        if self.is_tool_allowed(tool_name):
            return None

        logger.warning("Tool call %r blocked for subagent (not in allowed list: %s)", tool_name, self._allowed_patterns)
        allowed_str = ", ".join(self._allowed_patterns)
        return ToolMessage(
            content=(
                f"Tool call rejected: tool `{tool_name}` is restricted for this subagent. "
                f"Allowed tool patterns: [{allowed_str}]."
            ),
            name=tool_name,
            tool_call_id=request.tool_call.get("id", "") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "",
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        err = self._validate_tool_call(request)
        if err is not None:
            return err
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        err = self._validate_tool_call(request)
        if err is not None:
            return err
        return await handler(request)


__all__ = [
    "TOOL_ALIAS_MAP",
    "ToolFilterMiddleware",
]
