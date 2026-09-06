"""Memory guard middleware — prevents accidental corruption of memory files.

Ported from ``reference/opscode/src/opscode/memory/guard.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from k8s_autopilot.middleware.registry import register_middleware

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_GUARDED_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "delete_file", "execute"})


@register_middleware(name="memory_guard")
class ManagedMemoryGuardMiddleware(AgentMiddleware[Any, Any]):
    """Protects managed memory files (e.g. AGENTS.md) from destructive or corrupting edits."""

    def __init__(self, guarded_paths: Iterable[str | Path] = ()) -> None:
        super().__init__()
        resolved: set[Path] = set()
        for raw in guarded_paths:
            try:
                resolved.add(Path(raw).expanduser().resolve())
            except Exception:
                pass
        self._guarded: frozenset[Path] = frozenset(resolved)

    def _is_guarded_target(self, path_str: str) -> bool:
        if not path_str:
            return False
        try:
            target = Path(path_str).expanduser().resolve()
            for g in self._guarded:
                if target == g or g in target.parents:
                    return True
        except Exception:
            pass
        return False

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {})
        file_path = args.get("file_path") or args.get("path") or args.get("target") or ""

        if tool_name in _GUARDED_TOOLS and self._is_guarded_target(str(file_path)):
            logger.warning("MemoryGuard blocked modification to guarded memory: %s", file_path)
            return ToolMessage(
                content=f"Modification to protected memory file {file_path!r} is restricted.",
                name=tool_name,
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {})
        file_path = args.get("file_path") or args.get("path") or args.get("target") or ""

        if tool_name in _GUARDED_TOOLS and self._is_guarded_target(str(file_path)):
            logger.warning("MemoryGuard blocked modification to guarded memory: %s", file_path)
            return ToolMessage(
                content=f"Modification to protected memory file {file_path!r} is restricted.",
                name=tool_name,
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
        return await handler(request)
