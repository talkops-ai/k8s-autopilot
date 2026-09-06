"""Unit tests for K8s Autopilot State & Session Management Module."""

from __future__ import annotations

import pytest
from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage

from k8s_autopilot.state import (
    AgentState,
    BaseAgentState,
    K8sAgentState,
    SessionManager,
    ThreadInfo,
    create_checkpointer,
    delete_thread,
    find_similar_threads,
    format_path,
    format_relative_timestamp,
    format_timestamp,
    generate_thread_id,
    get_checkpointer,
    get_most_recent,
    list_threads,
    thread_exists,
)


def test_state_schema():
    assert AgentState is K8sAgentState
    assert "messages" in BaseAgentState.__annotations__
    assert "messages" in K8sAgentState.__annotations__


def test_thread_id_generation():
    tid = generate_thread_id()
    assert isinstance(tid, str)
    assert len(tid) == 36


def test_timestamp_and_path_formatting():
    assert format_timestamp(None) == ""
    assert format_relative_timestamp(None) == ""
    assert format_path(None) == ""
    assert format_path(str(Path.home())) == "~"


@pytest.mark.asyncio
async def test_create_checkpointer_default_sqlite(tmp_path: Path, monkeypatch):
    """Default checkpointer is SQLite."""
    db_file = tmp_path / "sessions.db"
    monkeypatch.setattr("k8s_autopilot.state.session.get_db_path", lambda: db_file)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("K8S_AUTOPILOT_POSTGRES_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async with create_checkpointer("auto") as cp:
        assert cp is not None

    assert db_file.exists()


@pytest.mark.asyncio
async def test_create_memory_checkpointer_unsupported():
    """Memory checkpointer is not supported and raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported checkpointer backend"):
        async with create_checkpointer("memory"):
            pass


@pytest.mark.asyncio
async def test_create_sqlite_checkpointer(tmp_path: Path, monkeypatch):
    """SQLite checkpointer creates DB and works via async context manager."""
    db_file = tmp_path / "sessions.db"
    monkeypatch.setattr("k8s_autopilot.state.session.get_db_path", lambda: db_file)

    async with create_checkpointer("sqlite") as cp:
        assert cp is not None

    assert db_file.exists()


@pytest.mark.asyncio
async def test_session_manager_crud(tmp_path: Path, monkeypatch):
    """Test SessionManager thread operations and message history retrieval."""
    db_file = tmp_path / "test_sessions.db"
    monkeypatch.setattr("k8s_autopilot.state.session.get_db_path", lambda: db_file)

    sm = SessionManager(db_path=db_file)
    tid = sm.generate_thread_id()
    assert isinstance(tid, str)

    # Empty DB
    threads = await sm.list_threads()
    assert threads == []

    # Get thread messages on empty thread returns empty list safely
    msgs = await sm.get_thread_messages(tid)
    assert msgs == []

    # Thread exists returns False on non-existent thread
    assert not await thread_exists(tid)
