"""Unit tests for A2AThreadHandler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from k8s_autopilot.server.thread_handler import A2AThreadHandler


@pytest.mark.asyncio
async def test_a2a_thread_handler_lifecycle() -> None:
    """Verify thread creation, active thread state, and deletion."""
    handler = A2AThreadHandler()
    assert handler.current_thread_id is None

    new_id = handler.create_thread()
    assert new_id is not None
    assert handler.current_thread_id == new_id

    handler.set_current_thread("custom-thread-id")
    assert handler.current_thread_id == "custom-thread-id"

    with patch("k8s_autopilot.server.thread_handler.delete_thread", new_callable=AsyncMock) as mock_del:
        mock_del.return_value = True
        success = await handler.delete_thread("custom-thread-id")
        assert success is True
        assert handler.current_thread_id is None


@pytest.mark.asyncio
async def test_a2a_thread_handler_list_threads() -> None:
    """Verify listing and searching threads."""
    handler = A2AThreadHandler(current_thread_id="thread-1")

    mock_threads = [
        {
            "thread_id": "thread-1",
            "initial_prompt": "Deploy redis cluster",
            "message_count": 5,
            "updated_at": "2026-08-23T10:00:00Z",
            "created_at": "2026-08-23T09:00:00Z",
        },
        {
            "thread_id": "thread-2",
            "initial_prompt": "Troubleshoot ingress crash",
            "message_count": 2,
            "updated_at": "2026-08-23T11:00:00Z",
            "created_at": "2026-08-23T10:30:00Z",
        },
    ]

    with patch("k8s_autopilot.server.thread_handler.list_threads", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = mock_threads

        # List all
        threads = await handler.list_threads()
        assert len(threads) == 2
        assert threads[0]["thread_id"] == "thread-1"
        assert threads[0]["is_active"] is True
        assert threads[1]["is_active"] is False

        # Filter with query
        filtered = await handler.list_threads(search_query="redis")
        assert len(filtered) == 1
        assert filtered[0]["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_a2a_thread_handler_get_history() -> None:
    """Verify fetching and parsing thread history from checkpointer."""
    handler = A2AThreadHandler()
    mock_checkpointer = AsyncMock()

    mock_state = MagicMock()
    mock_state.values = {
        "messages": [
            HumanMessage(content="Hello", id="m1"),
            AIMessage(content="Hi there!", id="m2"),
        ]
    }
    mock_checkpointer.aget.return_value = mock_state

    history = await handler.get_thread_history("thread-1", checkpointer=mock_checkpointer)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Hi there!"
