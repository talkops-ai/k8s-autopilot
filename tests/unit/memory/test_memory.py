"""Unit tests for memory discovery, store operations, and memory guard middleware."""

from __future__ import annotations

from pathlib import Path
from typing import Any
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
    """Verify ManagedMemoryGuardMiddleware protects managed blocks and deletion while allowing user edits."""
    managed_block = (
        "<!-- k8s_autopilot:onboarding-name:start -->\n"
        "- The user's preferred name is \"Sandeep\".\n"
        "<!-- k8s_autopilot:onboarding-name:end -->\n"
    )
    initial_content = f"# Protected Memory\n\n{managed_block}\n- Son's name: Ivaan\n"
    guarded_file = tmp_path / "AGENTS.md"
    guarded_file.write_text(initial_content, encoding="utf-8")

    guard = ManagedMemoryGuardMiddleware(guarded_paths=[guarded_file])

    # 1. Attempt to delete guarded file -> rejected outright
    req_delete = MagicMock()
    req_delete.tool_call = {
        "id": "tc-del",
        "name": "delete_file",
        "args": {"file_path": str(guarded_file)},
    }
    del_handler = MagicMock()
    del_res = guard.wrap_tool_call(req_delete, del_handler)
    assert isinstance(del_res, ToolMessage)
    assert del_res.status == "error"
    assert "must not be deleted" in del_res.content
    del_handler.assert_not_called()

    # 2. Edit that leaves managed block intact (adding user memory) -> allowed to proceed!
    req_add_memory = MagicMock()
    req_add_memory.tool_call = {
        "id": "tc-edit",
        "name": "edit_file",
        "args": {
            "file_path": str(guarded_file),
            "old_string": "- Son's name: Ivaan",
            "new_string": "- Son's name: Ivaan\n- Wife's name: Veena",
        },
    }
    def simulate_add_memory(req: Any) -> ToolMessage:
        content = guarded_file.read_text(encoding="utf-8")
        updated = content.replace("- Son's name: Ivaan", "- Son's name: Ivaan\n- Wife's name: Veena")
        guarded_file.write_text(updated, encoding="utf-8")
        return ToolMessage(content="File edited successfully", tool_call_id=req.tool_call["id"])

    edit_res = guard.wrap_tool_call(req_add_memory, simulate_add_memory)
    assert isinstance(edit_res, ToolMessage)
    assert edit_res.content == "File edited successfully"
    # Verify the edit succeeded on disk
    on_disk = guarded_file.read_text(encoding="utf-8")
    assert "Wife's name: Veena" in on_disk
    assert "The user's preferred name is \"Sandeep\"." in on_disk

    # 3. Edit that corrupts/deletes the managed block -> managed block is restored & error returned
    req_clobber = MagicMock()
    req_clobber.tool_call = {
        "id": "tc-clobber",
        "name": "write_file",
        "args": {"file_path": str(guarded_file), "content": "completely replaced memory"},
    }
    def simulate_clobber(req: Any) -> ToolMessage:
        guarded_file.write_text("completely replaced memory", encoding="utf-8")
        return ToolMessage(content="Wrote 25 bytes", tool_call_id=req.tool_call["id"])

    clobber_res = guard.wrap_tool_call(req_clobber, simulate_clobber)
    assert isinstance(clobber_res, ToolMessage)
    assert clobber_res.status == "error"
    assert "must not be edited" in clobber_res.content
    # Verify managed block was restored on disk
    restored_on_disk = guarded_file.read_text(encoding="utf-8")
    assert "The user's preferred name is \"Sandeep\"." in restored_on_disk

    # 4. Edit non-protected file -> allowed directly
    normal_file = tmp_path / "deployment.yaml"
    req_allowed = MagicMock()
    req_allowed.tool_call = {
        "id": "tc-normal",
        "name": "write_file",
        "args": {"file_path": str(normal_file), "content": "kind: Deployment"},
    }
    normal_handler = MagicMock()
    normal_handler.return_value = ToolMessage(content="Wrote 20 bytes", tool_call_id="tc-normal")

    result = guard.wrap_tool_call(req_allowed, normal_handler)
    assert isinstance(result, ToolMessage)
    assert result.content == "Wrote 20 bytes"
    normal_handler.assert_called_once_with(req_allowed)
