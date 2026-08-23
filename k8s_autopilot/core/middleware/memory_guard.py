"""Protect machine-managed memory blocks from agent edits."""

from __future__ import annotations

import asyncio
from k8s_autopilot.utils.logger import AgentLogger
import os
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from langchain.tools.tool_node import ToolCallRequest
    from langgraph.types import Command

logger = AgentLogger("MemoryGuardMW")

_GUARDED_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "delete"})

_REJECTION_MESSAGE = (
    "The region between the `deepagents:onboarding-name:start` and "
    "`deepagents:onboarding-name:end` markers in {path} is machine-managed and "
    "must not be edited. Your other changes to the file were kept, but the "
    "managed block was restored to its previous content. Do not modify content "
    "between those markers."
)

_RESTORE_FAILED_MESSAGE = (
    "The region between the `deepagents:onboarding-name:start` and "
    "`deepagents:onboarding-name:end` markers in {path} is machine-managed and "
    "must not be edited. Your edit changed it and the previous content could "
    "not be restored, so the managed block may now be corrupted. Do not modify "
    "content between those markers, and do not rely on this edit having "
    "succeeded."
)

_DELETE_REJECTION_MESSAGE = (
    "The guarded memory file {path} contains a machine-managed region between "
    "the `deepagents:onboarding-name:start` and `deepagents:onboarding-name:end` "
    "markers and must not be deleted. Do not delete this file or a parent "
    "directory that contains it."
)


# ---------------------------------------------------------------------------
# Onboarding name block helpers (Ported inline from onboarding.py)
# ---------------------------------------------------------------------------

ONBOARDING_NAME_MEMORY_START = "<!-- deepagents:onboarding-name:start -->"
ONBOARDING_NAME_MEMORY_END = "<!-- deepagents:onboarding-name:end -->"


def _upsert_onboarding_name_memory(existing: str, block: str) -> str:
    start = existing.find(ONBOARDING_NAME_MEMORY_START)
    end = existing.find(ONBOARDING_NAME_MEMORY_END)
    if start != -1 and end != -1 and start < end:
        end += len(ONBOARDING_NAME_MEMORY_END)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].strip()
        parts = [part for part in (prefix, block, suffix) if part]
        return "\n\n".join(parts).rstrip() + "\n"

    base = existing.rstrip()
    if not base:
        return f"## User Preferences\n\n{block}\n"
    if "## User Preferences" in base:
        return f"{base}\n\n{block}\n"
    return f"{base}\n\n## User Preferences\n\n{block}\n"


def extract_onboarding_name_block(text: str) -> str | None:
    start = text.find(ONBOARDING_NAME_MEMORY_START)
    end = text.find(ONBOARDING_NAME_MEMORY_END)
    if start == -1 or end == -1 or start >= end:
        return None
    return text[start : end + len(ONBOARDING_NAME_MEMORY_END)]


def strip_onboarding_name_markers(text: str) -> str:
    return text.replace(ONBOARDING_NAME_MEMORY_START, "").replace(
        ONBOARDING_NAME_MEMORY_END, ""
    )


class _RestoreOutcome(Enum):
    UNCHANGED = "unchanged"
    RESTORED = "restored"
    FAILED = "failed"


from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware


@register_middleware(name="memory_guard")
class ManagedMemoryGuardMiddleware(BaseAgentMiddleware):
    """Revert agent edits to the managed onboarding-name memory block."""

    def __init__(self, guarded_paths: Iterable[str | Path]) -> None:
        super().__init__()
        requested = list(guarded_paths)
        resolved: set[Path] = set()
        for raw in requested:
            try:
                resolved.add(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                logger.warning(f"Could not resolve guarded memory path {raw!r}", exc_info=True)
        self._guarded: frozenset[Path] = frozenset(resolved)
        if requested and not self._guarded:
            logger.error(f"ManagedMemoryGuardMiddleware resolved no guarded paths from {requested!r}; "
                "managed memory-block protection is disabled")

    def _guarded_path(self, request: ToolCallRequest) -> Path | None:
        tool_name = request.tool_call["name"]
        if tool_name not in _GUARDED_TOOLS:
            return None
        args = request.tool_call.get("args") or {}
        file_path = args.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return None
        try:
            resolved = Path(file_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            logger.warning(f"Could not resolve target path {file_path!r} for {tool_name}", exc_info=True)
            return None
        if tool_name == "delete":
            for guarded in self._guarded:
                if guarded.is_relative_to(resolved):
                    return guarded
            return None
        return resolved if resolved in self._guarded else None

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            logger.warning(f"Could not read guarded memory file {path}", exc_info=True)
            return None

    @staticmethod
    def _write(path: Path, content: str) -> None:
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    def _restore(self, path: Path, before: str, before_block: str) -> _RestoreOutcome:
        after = self._read(path)
        if after is None:
            logger.warning(f"Guarded memory file {path} is unreadable after edit; "
                "cannot restore managed block")
            return _RestoreOutcome.FAILED
        block_after = extract_onboarding_name_block(after)
        if block_after == before_block:
            return _RestoreOutcome.UNCHANGED
        if block_after is not None:
            source = after
        else:
            source = self._without_managed_block_edits(before, after, before_block)
            if source is None:
                logger.error(f"Could not locate previous managed block in {path}; leaving the "
                    "edited file untouched")
                return _RestoreOutcome.FAILED
            source = strip_onboarding_name_markers(source)
        restored = _upsert_onboarding_name_memory(source, before_block)
        if extract_onboarding_name_block(restored) != before_block:
            logger.error(f"Restored content for {path} did not reproduce the managed block; "
                "leaving the edited file untouched")
            return _RestoreOutcome.FAILED
        try:
            self._write(path, restored)
        except (OSError, UnicodeEncodeError):
            logger.warning(f"Could not restore managed memory block at {path}", exc_info=True)
            return _RestoreOutcome.FAILED
        return _RestoreOutcome.RESTORED

    @staticmethod
    def _line_range_for_block(before: str, before_block: str) -> tuple[int, int] | None:
        block_start = before.find(before_block)
        if block_start == -1:
            return None
        block_end = block_start + len(before_block)
        start_line: int | None = None
        end_line: int | None = None
        offset = 0
        for line_number, line in enumerate(before.splitlines(keepends=True)):
            line_end = offset + len(line)
            if start_line is None and offset <= block_start < line_end:
                start_line = line_number
            if offset < block_end <= line_end:
                end_line = line_number + 1
                break
            offset = line_end
        if start_line is None or end_line is None:
            return None
        return start_line, end_line

    @staticmethod
    def _without_managed_block_edits(
        before: str, after: str, before_block: str
    ) -> str | None:
        block_range = ManagedMemoryGuardMiddleware._line_range_for_block(
            before, before_block
        )
        if block_range is None:
            return None
        block_start, block_end = block_range
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        ranges: list[tuple[int, int]] = []
        matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
        for (
            tag,
            before_start,
            before_end,
            after_start,
            after_end,
        ) in matcher.get_opcodes():
            overlaps = before_start < block_end and block_start < before_end
            if tag == "insert":
                if block_start < before_start < block_end:
                    ranges.append((after_start, after_end))
                continue
            if not overlaps or tag == "delete":
                continue
            if tag == "equal":
                start = max(before_start, block_start)
                end = min(before_end, block_end)
                ranges.append(
                    (
                        after_start + start - before_start,
                        after_start + end - before_start,
                    )
                )
            else:
                ranges.append((after_start, after_end))

        if not ranges:
            return after
        parts: list[str] = []
        cursor = 0
        for start, end in sorted(ranges):
            range_start = start
            range_end = end
            if range_start < cursor:
                range_end = max(range_end, cursor)
                range_start = cursor
            parts.extend(after_lines[cursor:range_start])
            cursor = range_end
        parts.extend(after_lines[cursor:])
        return "".join(parts)

    @staticmethod
    def _error(
        request: ToolCallRequest, path: Path, *, restore_failed: bool
    ) -> ToolMessage:
        template = _RESTORE_FAILED_MESSAGE if restore_failed else _REJECTION_MESSAGE
        return ToolMessage(
            content=template.format(path=path),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    @staticmethod
    def _reject_delete(path: Path, before: str | None) -> bool:
        if before is not None:
            return extract_onboarding_name_block(before) is not None
        return path.exists()

    @staticmethod
    def _delete_error(request: ToolCallRequest, path: Path) -> ToolMessage:
        return ToolMessage(
            content=_DELETE_REJECTION_MESSAGE.format(path=path),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def _result_after_restore(
        self,
        request: ToolCallRequest,
        path: Path,
        before: str,
        before_block: str,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        outcome = self._restore(path, before, before_block)
        if outcome is _RestoreOutcome.UNCHANGED:
            return result
        return self._error(
            request, path, restore_failed=outcome is _RestoreOutcome.FAILED
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        path = self._guarded_path(request)
        if path is None:
            return handler(request)
        before = self._read(path)
        if request.tool_call["name"] == "delete":
            if self._reject_delete(path, before):
                return self._delete_error(request, path)
            return handler(request)
        before_block = (
            extract_onboarding_name_block(before) if before is not None else None
        )
        if before is None or before_block is None:
            return handler(request)
        result = handler(request)
        return self._result_after_restore(request, path, before, before_block, result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        path = await asyncio.to_thread(self._guarded_path, request)
        if path is None:
            return await handler(request)
        before = await asyncio.to_thread(self._read, path)
        if request.tool_call["name"] == "delete":
            if await asyncio.to_thread(self._reject_delete, path, before):
                return self._delete_error(request, path)
            return await handler(request)
        before_block = (
            extract_onboarding_name_block(before) if before is not None else None
        )
        if before is None or before_block is None:
            return await handler(request)
        result = await handler(request)
        return await asyncio.to_thread(
            self._result_after_restore, request, path, before, before_block, result
        )
