"""Unit tests for memory discovery, store operations, and memory guard middleware."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.store.memory import InMemoryStore

from k8s_autopilot.memory.registry import MemoryRegistry
from k8s_autopilot.middleware.memory_guard import ManagedMemoryGuardMiddleware


@pytest.mark.asyncio
async def test_memory_registry_store_operations() -> None:
    """Verify thread-isolated memory save, get, and list operations."""
    store = InMemoryStore()
    registry = MemoryRegistry(store=store)

    await registry.save_memory("thread-1", "user_preference", "always use dry-run")
    val = await registry.get_memory("thread-1", "user_preference")
    assert val == "always use dry-run"

    # Isolation check: thread-2 should not see thread-1's memory
    val2 = await registry.get_memory("thread-2", "user_preference")
    assert val2 is None

    # List memories
    items = await registry.list_memories("thread-1")
    assert len(items) == 1
    assert items[0]["key"] == "user_preference"
    assert items[0]["content"] == "always use dry-run"


def test_memory_guard_middleware(tmp_path: Path) -> None:
    """Verify ManagedMemoryGuardMiddleware blocks writes/edits to protected memory paths."""
    guarded_file = tmp_path / "AGENTS.md"
    guarded_file.write_text("# Protected Memory\n", encoding="utf-8")

    guard = ManagedMemoryGuardMiddleware(guarded_paths=[guarded_file])

    # 1. Attempt to edit protected file -> blocked
    req_blocked = MagicMock()
    req_blocked.tool_call = {
        "id": "tc-1",
        "name": "write_file",
        "args": {"file_path": str(guarded_file), "content": "corrupted"},
    }
    handler = MagicMock()

    result = guard.wrap_tool_call(req_blocked, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "protected memory file" in result.content
    handler.assert_not_called()

    # 2. Edit non-protected file -> allowed
    normal_file = tmp_path / "deployment.yaml"
    req_allowed = MagicMock()
    req_allowed.tool_call = {
        "id": "tc-2",
        "name": "write_file",
        "args": {"file_path": str(normal_file), "content": "kind: Deployment"},
    }
    handler.return_value = ToolMessage(content="Wrote 20 bytes", tool_call_id="tc-2")

    result = guard.wrap_tool_call(req_allowed, handler)
    assert result.content == "Wrote 20 bytes"
    handler.assert_called_once_with(req_allowed)
