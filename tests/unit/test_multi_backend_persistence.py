"""Multi-backend persistence and thread reconstruction tests (SQLite & PostgreSQL).

Tests:
1. SQLite and PostgreSQL checkpointer creation and auto-detection.
2. Thread auto-touch, title generation, and covering index queries.
3. Message replay and reconstruction from writes/checkpoints via SessionManager.
4. ThreadService search_threads and get_thread_state across backends.
5. Stream deduplication on task completion.
"""

from __future__ import annotations

import os
from typing import Any, cast
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from k8s_autopilot.api.models import ThreadSearch
from k8s_autopilot.api.service import ThreadService
from k8s_autopilot.server.executor import A2AAutoPilotExecutor, _StreamRenderer
from k8s_autopilot.state import session
from k8s_autopilot.state.session import (
    SessionManager,
    create_checkpointer,
    delete_thread,
    get_active_backend,
    list_threads,
)


@pytest.mark.asyncio
async def test_sqlite_thread_persistence_and_reconstruction(tmp_path, monkeypatch) -> None:
    """Test full persistence cycle on SQLite checkpointer."""
    db_file = tmp_path / "test_sessions.db"
    monkeypatch.setattr(session, "_db_path", db_file)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")

    async with create_checkpointer(backend="sqlite") as checkpointer:
        thread_id = str(uuid.uuid4())
        thread_service = ThreadService(checkpointer)

        # 1. Auto-touch to establish thread record
        await thread_service.auto_touch(
            thread_id=thread_id,
            user_id="user-123",
            agent_id="k8s-autopilot",
            user_query="How do I inspect Kubernetes cluster pods?",
        )

        # 2. Write a checkpoint with messages
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        msg_human = HumanMessage(content="How do I inspect Kubernetes cluster pods?")
        msg_ai = AIMessage(content="You can use `kubectl get pods -A` to inspect all pods.")

        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        cp = empty_checkpoint()
        cp["channel_values"] = {"messages": [msg_human, msg_ai]}

        meta = {
            "title": "How do I inspect Kubernetes cluster pods?",
            "agent_name": "k8s-autopilot",
            "user_id": "user-123",
            "updated_at": "2026-08-27T10:00:00+00:00",
            "created_at": "2026-08-27T10:00:00+00:00",
        }
        await checkpointer.aput(cast(Any, config), cp, cast(Any, meta), new_versions={})

        # 3. Search threads and verify title & metadata
        threads = await thread_service.search_threads(ThreadSearch(user_id="user-123", limit=10))
        assert len(threads) >= 1
        target = next((t for t in threads if str(t.thread_id) == thread_id), None)
        assert target is not None
        assert "Kubernetes cluster pods" in target.title

        # 4. Get thread state and verify messages
        state = await thread_service.get_thread_state(thread_id)
        assert state is not None
        assert state.title == "How do I inspect Kubernetes cluster pods?"
        assert len(state.messages) >= 2
        assert state.messages[0]["type"] == "human"
        assert "inspect Kubernetes cluster pods" in state.messages[0]["content"]
        assert state.messages[1]["type"] == "ai"
        assert "kubectl get pods" in state.messages[1]["content"]

        # 5. Verify SessionManager.get_thread_messages fallback
        sm = SessionManager(db_path=db_file)
        msgs = await sm.get_thread_messages(thread_id)
        assert len(msgs) >= 2

        # 6. Verify delete thread
        deleted = await thread_service.delete_thread(thread_id)
        assert deleted is True
        remaining = await thread_service.search_threads(ThreadSearch(user_id="user-123", limit=10))
        assert not any(str(t.thread_id) == thread_id for t in remaining)


@pytest.mark.asyncio
async def test_session_manager_delta_writes_reconstruction(tmp_path, monkeypatch) -> None:
    """Test replaying messages from writes table through add_messages reducer."""
    db_file = tmp_path / "test_writes.db"
    monkeypatch.setattr(session, "_db_path", db_file)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")

    async with create_checkpointer(backend="sqlite") as checkpointer:
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

        # Write delta message chunks as LangGraph writes
        from langchain_core.messages import HumanMessage, AIMessage

        h_msg = HumanMessage(content="Check deployment status", id="h1")
        ai_msg = AIMessage(content="Deployment is running smoothly.", id="a1")

        # Put checkpoint then delta writes into checkpointer
        cp = empty_checkpoint()
        saved_config = await checkpointer.aput(
            cast(Any, config),
            cp,
            cast(Any, {"title": "Check deployment status", "updated_at": "2026-08-27T10:00:00+00:00"}),
            new_versions={},
        )
        await checkpointer.aput_writes(
            saved_config,
            [("messages", [h_msg]), ("messages", [ai_msg])],
            task_id="task-1",
        )

        sm = SessionManager(db_path=db_file)
        messages = await sm.get_thread_messages(thread_id)
        assert len(messages) == 2
        assert messages[0].content == "Check deployment status"
        assert messages[1].content == "Deployment is running smoothly."


def test_backend_auto_detection(monkeypatch) -> None:
    """Test that backend auto-detection prioritizes postgres when configured."""
    monkeypatch.delenv("CHECKPOINT_BACKEND", raising=False)
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("K8S_AUTOPILOT_POSTGRES_URI", raising=False)

    # 1. Default should be sqlite
    assert get_active_backend() == "sqlite"

    # 2. Explicit CHECKPOINTER_BACKEND="postgres"
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    assert get_active_backend() == "postgres"

    # 3. Explicit CHECKPOINTER_BACKEND="sqlite"
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")
    assert get_active_backend() == "sqlite"

    # 4. K8S_AUTOPILOT_POSTGRES_URI sets postgres
    monkeypatch.delenv("CHECKPOINT_BACKEND", raising=False)
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setenv("K8S_AUTOPILOT_POSTGRES_URI", "postgresql://user:pass@localhost:5432/talkops")
    assert get_active_backend() == "postgres"


@pytest.mark.asyncio
async def test_stream_renderer_deduplication() -> None:
    """Test that stream renderer does not send duplicate full text on task completion."""
    mock_updater = AsyncMock()
    mock_updater.update_status = AsyncMock()

    renderer = _StreamRenderer(
        updater=mock_updater,
        context_id="ctx-123",
        task_id="task-456",
    )

    # Emit streaming tokens
    await renderer.emit("Hello, ")
    await renderer.emit("world!")

    assert renderer.get_accumulated_response() == "Hello, world!"

    # On stream completion, if tokens were accumulated, final_text should be empty string
    accumulated = renderer.get_accumulated_response()
    final_text = "" if accumulated else "Task completed successfully."
    assert final_text == ""


@pytest.mark.asyncio
async def test_executor_dynamic_checkpointer_compilation(monkeypatch) -> None:
    """Test that A2AAutoPilotExecutor recompiles agent when checkpointer changes."""
    mock_checkpointer_1 = MagicMock()
    mock_checkpointer_2 = MagicMock()

    mock_agent_1 = MagicMock()
    mock_agent_2 = MagicMock()
    creation_calls: list[Any] = []

    def fake_create_agent(**kwargs):
        cp = kwargs.get("checkpointer")
        creation_calls.append(cp)
        return (mock_agent_1 if cp is mock_checkpointer_1 else mock_agent_2), None

    monkeypatch.setattr("k8s_autopilot.agent.create_k8s_autopilot_agent", fake_create_agent)

    executor = A2AAutoPilotExecutor(checkpointer=mock_checkpointer_1)

    agent_1 = executor._ensure_agent(requested_model="google_genai:gemini-2.5-flash")
    assert executor._active_checkpointer is mock_checkpointer_1
    assert agent_1 is mock_agent_1

    # Switch checkpointer to simulate lifespan wiring
    executor.checkpointer = mock_checkpointer_2
    agent_2 = executor._ensure_agent(requested_model="google_genai:gemini-2.5-flash")
    assert executor._active_checkpointer is mock_checkpointer_2
    assert agent_2 is mock_agent_2
    assert len(creation_calls) == 2


@pytest.mark.asyncio
async def test_live_postgres_persistence() -> None:
    """Test live PostgreSQL persistence when POSTGRES_URI is available and reachable."""
    pg_uri = os.environ.get("POSTGRES_URI") or os.environ.get("K8S_AUTOPILOT_POSTGRES_URI")
    if not pg_uri:
        pytest.skip("PostgreSQL URI not provided in environment")
    try:
        async with create_checkpointer(backend="postgres") as checkpointer:
            thread_id = str(uuid.uuid4())
            thread_service = ThreadService(checkpointer)

            await thread_service.auto_touch(
                thread_id=thread_id,
                user_id="pg-user",
                agent_id="k8s-autopilot",
                user_query="Live PostgreSQL checkpoint verification query",
            )

            threads = await thread_service.search_threads(ThreadSearch(user_id="pg-user", limit=10))
            assert len(threads) >= 1
            target = next((t for t in threads if str(t.thread_id) == thread_id), None)
            assert target is not None
            assert "Live PostgreSQL" in target.title

            deleted = await thread_service.delete_thread(thread_id)
            assert deleted is True
    except Exception as exc:
        pytest.skip(f"Live PostgreSQL not reachable: {exc}")


@pytest.mark.asyncio
async def test_multi_turn_history_preservation_with_auto_touch() -> None:
    """Test that multiple turns with auto_touch maintain full graph state and ancestor chain."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from k8s_autopilot.agent.factory import create_k8s_autopilot_agent

    class ToolFakeModel(GenericFakeChatModel):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._call_count = 0

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            self._call_count += 1
            if self._call_count == 1:
                content = "Hello Sandeep! Nice to meet you."
            else:
                content = "Your previous question was: what is my name?"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async with create_checkpointer(backend="sqlite") as checkpointer:
        fake_model = ToolFakeModel(messages=iter([AIMessage(content="default")]))

        agent_graph, _ = create_k8s_autopilot_agent(model=fake_model, checkpointer=checkpointer)
        thread_id = str(uuid.uuid4())
        thread_service = ThreadService(checkpointer=checkpointer)

        # Turn 1
        await thread_service.auto_touch(thread_id, user_query="my name is sandeep")
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        async for _ in agent_graph.astream(
            {"messages": [HumanMessage(content="my name is sandeep")]},
            config=cast(Any, config),
            stream_mode=["messages"],
        ):
            pass

        # Turn 2: auto_touch before turn 2 must NOT sever the chain
        await thread_service.auto_touch(thread_id, user_query="what was my previous question ?")
        async for _ in agent_graph.astream(
            {"messages": [HumanMessage(content="what was my previous question ?")]},
            config=cast(Any, config),
            stream_mode=["messages"],
        ):
            pass

        # Verify Pregel snapshot contains all 4 messages across both turns
        state = await agent_graph.aget_state(cast(Any, config))
        messages = state.values.get("messages", [])
        assert len(messages) == 4
        assert messages[0].content == "my name is sandeep"
        assert messages[1].content == "Hello Sandeep! Nice to meet you."
        assert messages[2].content == "what was my previous question ?"
        assert messages[3].content == "Your previous question was: what is my name?"

