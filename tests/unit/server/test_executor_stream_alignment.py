"""Unit tests for executor stream compilation, resume commands, and approval mode alignment."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from langgraph.types import Command

from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.security.approval_mode import ApprovalMode
from k8s_autopilot.server.executor import A2AAutoPilotExecutor


@pytest.fixture
def executor():
    mock_agent = MagicMock()
    mock_agent.name = "test-agent"
    return A2AAutoPilotExecutor(agent=mock_agent)


class TestExtractApprovalMode:
    """Test _extract_approval_mode against various RequestContext configurations."""

    def test_extract_from_configuration_dict(self, executor):
        ctx = MagicMock(spec=RequestContext)
        ctx.configuration = {"approval_mode": "auto"}
        ctx.message = None
        ctx.metadata = None
        assert executor._extract_approval_mode(ctx) == "auto"

    def test_extract_from_message_metadata(self, executor):
        ctx = MagicMock(spec=RequestContext)
        ctx.configuration = None
        msg = MagicMock(spec=Message)
        msg.metadata = {"approval_mode": "yolo"}
        ctx.message = msg
        ctx.metadata = None
        assert executor._extract_approval_mode(ctx) == "yolo"

    def test_extract_from_request_metadata(self, executor):
        ctx = MagicMock(spec=RequestContext)
        ctx.configuration = None
        ctx.message = None
        ctx.metadata = {"approval_mode": "manual"}
        assert executor._extract_approval_mode(ctx) == "manual"

    def test_extract_invalid_or_missing_returns_none(self, executor):
        ctx = MagicMock(spec=RequestContext)
        ctx.configuration = {"approval_mode": "invalid_mode"}
        ctx.message = None
        ctx.metadata = None
        assert executor._extract_approval_mode(ctx) is None


class TestResolveActionToQuery:
    """Test that _resolve_action_to_query never returns raw decision strings."""

    def test_decision_auto_serializes_as_json(self, executor):
        user_action = {
            "action": "hitl_response",
            "context": {"decision": "auto"},
        }
        res = executor._resolve_action_to_query(user_action)
        parsed = json.loads(res)
        assert parsed["action"] == "hitl_response"
        assert parsed["decision"] == "auto"
        assert res != "auto"

    def test_decision_auto_approve_all_serializes_as_json(self, executor):
        user_action = {
            "action": "hitl_response",
            "context": {"decision": "auto_approve_all"},
        }
        res = executor._resolve_action_to_query(user_action)
        parsed = json.loads(res)
        assert parsed["action"] == "hitl_response"
        assert parsed["decision"] == "auto_approve_all"

    def test_decision_with_context_attributes(self, executor):
        user_action = {
            "action": "hitl_response",
            "context": {"decision": "approve", "reason": "verified safe"},
        }
        res = executor._resolve_action_to_query(user_action)
        parsed = json.loads(res)
        assert parsed["action"] == "hitl_response"
        assert parsed["decision"] == "approve"
        assert parsed["reason"] == "verified safe"


class TestWrapResume:
    """Test _wrap_resume wrapping and auto mode persistence."""

    @pytest.mark.asyncio
    async def test_wrap_resume_with_pending_interrupt_auto(self, executor):
        agent_graph = MagicMock()
        mock_state = MagicMock()
        mock_state.tasks = [
            MagicMock(
                interrupts=[
                    MagicMock(
                        id="int-123",
                        value={"action_requests": [{"name": "helm_install"}]},
                    )
                ]
            )
        ]
        agent_graph.aget_state = AsyncMock(return_value=mock_state)

        task = Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        config = {"configurable": {"thread_id": "ctx-1"}}
        query = json.dumps({"action": "hitl_response", "decision": "auto_approve_all"})

        with patch("k8s_autopilot.security.approval_mode.awrite_approval_mode", new_callable=AsyncMock) as mock_write:
            res = await executor._wrap_resume(agent_graph, config, task, query)
            assert isinstance(res, Command)
            assert "int-123" in res.resume
            assert res.resume["int-123"] == {"decisions": [{"type": "approve"}]}
            mock_write.assert_awaited_once_with(agent_graph, "ctx-1", mode="auto")

    @pytest.mark.asyncio
    async def test_wrap_resume_fallback_auto_string(self, executor):
        agent_graph = MagicMock()
        mock_state = MagicMock()
        mock_state.tasks = []
        agent_graph.aget_state = AsyncMock(return_value=mock_state)

        task = Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED))
        config = {"configurable": {"thread_id": "ctx-1"}}
        query = "auto"

        with patch("k8s_autopilot.security.approval_mode.awrite_approval_mode", new_callable=AsyncMock) as mock_write:
            res = await executor._wrap_resume(agent_graph, config, task, query)
            assert isinstance(res, Command)
            assert res.resume == "approve"
            mock_write.assert_awaited_once_with(agent_graph, "ctx-1", mode="auto")


class TestBareModeInterception:
    """Test execute() bare mode command interception."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", ["/auto", "auto", "/manual", "manual", "/yolo", "yolo"])
    async def test_bare_mode_command_completes_early(self, executor, cmd):
        ctx = MagicMock(spec=RequestContext)
        ctx.configuration = None
        ctx.metadata = None
        ctx.get_user_input.return_value = cmd
        msg = Message(
            role=Role.ROLE_USER,
            parts=[Part(text=cmd)],
            message_id="msg-1",
        )
        ctx.message = msg
        ctx.current_task = None

        event_queue = MagicMock(spec=EventQueue)
        event_queue.enqueue_event = AsyncMock()

        orig_mode = get_settings().approval_mode
        try:
            with patch("k8s_autopilot.security.approval_mode.awrite_approval_mode", new_callable=AsyncMock) as mock_write, \
                 patch.object(executor, "_stream_agent", new_callable=AsyncMock) as mock_stream:
                await executor.execute(ctx, event_queue)
                mock_stream.assert_not_called()
                clean = cmd.lstrip("/")
                assert get_settings().approval_mode == clean
        finally:
            get_settings().approval_mode = orig_mode


class TestStreamContextInjection:
    """Test that _stream_agent does not pollute input_data and injects enriched_context."""

    @pytest.mark.asyncio
    async def test_stream_agent_passes_enriched_context(self, executor):
        agent_graph = MagicMock()

        async def empty_stream(*args, **kwargs):
            if False:
                yield ()

        agent_graph.astream = MagicMock(side_effect=empty_stream)

        task = Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        updater = MagicMock()
        updater.update_status = AsyncMock()
        updater.complete = AsyncMock()
        event_queue = MagicMock()

        with patch.object(executor, "_init_stream_telemetry") as mock_init:
            from k8s_autopilot.server.executor import _StreamTelemetryState

            mock_telemetry = _StreamTelemetryState(
                current_approval_mode="auto",
                active_model="gemini-3.7-flash",
                active_effort="medium",
            )
            mock_init.return_value = mock_telemetry

            await executor._stream_agent(
                query="list pods",
                task=task,
                updater=updater,
                event_queue=event_queue,
                context_id="ctx-1",
                use_ui=False,
                requested_model="gemini-3.7-flash",
                requested_effort="medium",
                requested_mode="auto",
                agent_graph=agent_graph,
                config={"configurable": {"thread_id": "ctx-1"}},
            )

            agent_graph.astream.assert_called_once()
            call_args, call_kwargs = agent_graph.astream.call_args

            # Verify input_data is not polluted with approval_mode
            input_data = call_args[0]
            assert "approval_mode" not in input_data
            assert "messages" in input_data

            # Verify context=enriched_context was passed
            assert "context" in call_kwargs
            ctx_passed = call_kwargs["context"]
            assert ctx_passed["thread_id"] == "ctx-1"
            assert ctx_passed["approval_mode"] == "auto"
            assert "turn_id" in ctx_passed

    @pytest.mark.asyncio
    async def test_stream_agent_with_real_cli_context_schema(self, executor):
        """Verify enriched_context cleanly initializes CLIContextSchema without unexpected keyword argument errors."""
        from k8s_autopilot.agent.config import CLIContextSchema

        agent_graph = MagicMock()
        agent_graph.context_schema = CLIContextSchema

        captured_context = None

        async def capture_stream(*args, **kwargs):
            nonlocal captured_context
            captured_context = kwargs.get("context")
            # Verify CLIContextSchema can be instantiated without error
            schema_inst = CLIContextSchema(**captured_context)
            assert schema_inst.approval_mode == "auto"
            assert schema_inst.reasoning_effort == "medium"
            if False:
                yield ()

        agent_graph.astream = MagicMock(side_effect=capture_stream)

        task = Task(id="task-2", context_id="ctx-2", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        updater = MagicMock()
        updater.update_status = AsyncMock()
        updater.complete = AsyncMock()
        event_queue = MagicMock()

        await executor._stream_agent(
            query="get nodes",
            task=task,
            updater=updater,
            event_queue=event_queue,
            context_id="ctx-2",
            use_ui=False,
            requested_model="gemini-3.7-flash",
            requested_effort="medium",
            requested_mode="auto",
            agent_graph=agent_graph,
            config={"configurable": {"thread_id": "ctx-2"}},
        )

        assert captured_context is not None
        assert captured_context["approval_mode"] == "auto"
        assert captured_context["reasoning_effort"] == "medium"
