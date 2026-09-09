"""Unit tests for Thread Token Usage and Pricing Telemetry across namespaces."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from k8s_autopilot.server.executor import A2AAutoPilotExecutor, _StreamContext, _StreamTelemetryState
from k8s_autopilot.state.session import SessionManager, _get_jsonplus_serializer


@pytest.mark.asyncio
async def test_process_message_telemetry_accumulation():
    """Verify that _process_message_telemetry accumulates tokens and cost across calls without duplicate counting."""
    executor = A2AAutoPilotExecutor()
    telemetry = _StreamTelemetryState(
        active_model="google_genai:gemini-3.7-flash",
        active_effort="medium",
    )
    ctx = _StreamContext(
        task=None,  # type: ignore[arg-type]
        updater=None,  # type: ignore[arg-type]
        event_queue=None,  # type: ignore[arg-type]
        context_id="test-ctx-123",
        use_ui=False,
        renderer=None,  # type: ignore[arg-type]
        telemetry=telemetry,
    )

    msg1 = AIMessage(
        content="Step 1 response",
        id="msg-run-001",
        response_metadata={"model_name": "gemini-3.7-flash"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
    )

    # 1. Process msg1
    executor._process_message_telemetry(msg1, ctx)
    assert telemetry.cumulative_input_tokens == 1000
    assert telemetry.cumulative_output_tokens == 100
    # Cost is not calculated un-cachedly in _process_message_telemetry
    assert telemetry.cumulative_cost_usd == 0.0

    # 2. Duplicate processing of msg1 should NOT increment tokens
    executor._process_message_telemetry(msg1, ctx)
    assert telemetry.cumulative_input_tokens == 1000
    assert telemetry.cumulative_output_tokens == 100
    assert telemetry.cumulative_cost_usd == 0.0

    # 3. Subagent message accumulates tokens
    msg_subagent = AIMessage(
        content="Subagent step",
        id="msg-subagent-002",
        response_metadata={"model_name": "gemini-3.7-flash"},
        usage_metadata={"input_tokens": 5000, "output_tokens": 250, "total_tokens": 5250},
    )
    executor._process_message_telemetry(msg_subagent, ctx)
    assert telemetry.cumulative_input_tokens == 6000
    assert telemetry.cumulative_output_tokens == 350
    assert telemetry.cumulative_cost_usd == 0.0


@pytest.mark.asyncio
async def test_handle_custom_chunk_session_cost():
    """Verify that session_cost events authoritatively set cumulative_cost_usd without max lock-in."""
    executor = A2AAutoPilotExecutor()
    telemetry = _StreamTelemetryState()
    ctx = _StreamContext(
        task=None,  # type: ignore[arg-type]
        updater=None,  # type: ignore[arg-type]
        event_queue=None,  # type: ignore[arg-type]
        context_id="test-ctx-123",
        use_ui=False,
        renderer=None,  # type: ignore[arg-type]
        telemetry=telemetry,
    )

    # Initial session_cost from CostTrackingMiddleware
    await executor._handle_custom_chunk({"type": "session_cost", "total": 0.16401075}, ctx)
    assert ctx.telemetry.cumulative_cost_usd == 0.16401075

    # Direct authoritative update (without max lock-in)
    await executor._handle_custom_chunk({"type": "session_cost", "total": 0.1805}, ctx)
    assert ctx.telemetry.cumulative_cost_usd == 0.1805


@pytest.mark.asyncio
async def test_get_thread_token_usage_sqlite(tmp_path):
    """Test get_thread_token_usage_and_cost with SQLite storage across namespaces."""
    import aiosqlite

    db_file = tmp_path / "test_telemetry.db"
    serde = await _get_jsonplus_serializer()

    async with aiosqlite.connect(db_file) as conn:
        await conn.execute(
            """
            CREATE TABLE writes (
                thread_id TEXT,
                checkpoint_ns TEXT,
                checkpoint_id TEXT,
                task_id TEXT,
                idx INTEGER,
                channel TEXT,
                type TEXT,
                value BLOB
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT,
                checkpoint_ns TEXT,
                checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB
            )
            """
        )

        # Message 1 in root namespace
        m1 = AIMessage(
            content="Hello",
            id="m-root-1",
            usage_metadata={"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280},
        )
        t1, b1 = serde.dumps_typed(m1)
        await conn.execute(
            "INSERT INTO writes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("thread-1", "", "cp-1", "task-1", 0, "messages", t1, b1),
        )

        # Message 2 in subagent namespace
        m2 = AIMessage(
            content="Subagent action",
            id="m-sub-2",
            usage_metadata={"input_tokens": 3400, "output_tokens": 120, "total_tokens": 3520},
        )
        t2, b2 = serde.dumps_typed(m2)
        await conn.execute(
            "INSERT INTO writes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("thread-1", "tools:subagent_operator", "cp-2", "task-2", 0, "messages", t2, b2),
        )

        # Root checkpoint with _session_cost_usd
        cp_dict = {
            "channel_values": {
                "_session_cost_usd": 0.045,
            }
        }
        tcp, bcp = serde.dumps_typed(cp_dict)
        await conn.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
            ("thread-1", "", "cp-final", tcp, bcp),
        )
        await conn.commit()

    sm = SessionManager()
    orig_path = sm._db_path
    orig_pg = sm._postgres_uri
    sm._db_path = db_file
    sm._postgres_uri = None

    try:
        inp, outp, cost, msg_ids = await sm.get_thread_token_usage_and_cost("thread-1")
        assert inp == 4600
        assert outp == 200
        assert inp + outp == 4800
        assert cost == 0.045
        assert set(msg_ids) == {"m-root-1", "m-sub-2"}
    finally:
        sm._db_path = orig_path
        sm._postgres_uri = orig_pg
