"""Unit tests for A2AAutoPilotExecutor and Pregel stream translation."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf import struct_pb2
from langchain_core.messages import (
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command

from k8s_autopilot.server.executor import A2AAutoPilotExecutor, _StreamRenderer


# ---------------------------------------------------------------------------
# Helpers & Mocks
# ---------------------------------------------------------------------------

class MockEventQueue(EventQueue):
    """Event queue collecting all dispatched events for assertions."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def _make_mock_task(
    state: TaskState = TaskState.TASK_STATE_SUBMITTED,
    context_id: str = "test-ctx-123",
    task_id: str = "test-task-456",
) -> Task:
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=state),
    )


def _make_mock_request_context(
    user_input: str = "",
    parts: list[Part] | None = None,
    task: Task | None = None,
) -> RequestContext:
    mock_msg = Message(
        role=Role.ROLE_USER,
        parts=parts or ([Part(text=user_input)] if user_input else []),
        message_id="msg-123",
        context_id="test-ctx-123",
    )
    ctx = MagicMock(spec=RequestContext)
    ctx.get_user_input.return_value = user_input
    ctx.message = mock_msg
    ctx.current_task = task
    ctx.extensions = []
    return ctx


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

def test_extract_query_plain_text():
    """Verify plain text query is extracted directly."""
    executor = A2AAutoPilotExecutor(agent=MagicMock())
    ctx = _make_mock_request_context(user_input="List all pods in default namespace")
    query = executor._extract_query(ctx)
    assert query == "List all pods in default namespace"


def test_extract_user_action_hitl():
    """Verify A2UI userAction extraction for HITL response."""
    executor = A2AAutoPilotExecutor(agent=MagicMock())

    # Build protobuf DataPart containing userAction
    val = struct_pb2.Value()
    val.struct_value.update({
        "name": "hitl_response",
        "context": [
            {"key": "decision", "value": {"literalString": "approve"}},
            {"key": "repository", "value": {"literalString": "my-repo"}},
        ],
    })
    part = Part(data=val)

    ctx = _make_mock_request_context(user_input="", parts=[part])
    query = executor._extract_query(ctx)
    assert query is not None

    parsed = json.loads(query)
    assert parsed["decision"] == "approve"
    assert parsed["repository"] == "my-repo"


@pytest.mark.asyncio
async def test_wrap_resume_input_required():
    """Verify query is wrapped in Command(resume=...) when task is in input_required state."""
    task = _make_mock_task(state=TaskState.TASK_STATE_INPUT_REQUIRED)
    config = {"configurable": {"thread_id": "thread-1"}}
    wrapped = await A2AAutoPilotExecutor._wrap_resume(None, config, task, "approve")
    assert isinstance(wrapped, Command)
    assert wrapped.resume == "approve"

    # With JSON dict string
    wrapped_json = await A2AAutoPilotExecutor._wrap_resume(None, config, task, '{"decision": "reject"}')
    assert isinstance(wrapped_json, Command)
    assert wrapped_json.resume == {"decision": "reject"}


@pytest.mark.asyncio
async def test_wrap_resume_structured_answers():
    """Verify structured ask_user and goal response JSON strings wrap as Command even if task is submitted."""
    task = _make_mock_task(state=TaskState.TASK_STATE_SUBMITTED)
    config = {"configurable": {"thread_id": "thread-1"}}
    wrapped = await A2AAutoPilotExecutor._wrap_resume(
        None, config, task, '{"status": "answered", "answers": ["Kubernetes", ""]}'
    )
    assert isinstance(wrapped, Command)
    assert wrapped.resume == {"status": "answered", "answers": ["Kubernetes", ""]}


@pytest.mark.asyncio
async def test_wrap_resume_not_paused():
    """Verify normal query is NOT wrapped when task is not paused and no pending interrupts."""
    task = _make_mock_task(state=TaskState.TASK_STATE_WORKING)
    config = {"configurable": {"thread_id": "thread-1"}}
    wrapped = await A2AAutoPilotExecutor._wrap_resume(None, config, task, "hello")
    assert wrapped == "hello"



@pytest.mark.asyncio
async def test_executor_text_streaming():
    """Verify execution streams token chunks and marks task complete."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        assert "thread_id" in config.get("configurable", {})
        yield ("messages", (AIMessageChunk(content="Listing "), {}))
        yield ("messages", (AIMessageChunk(content="pods..."), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Get pods", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Check status updates in event_queue
    status_updates = [
        e for e in event_queue.events if hasattr(e, "status") and hasattr(e.status, "state")
    ]
    assert len(status_updates) >= 2

    # Verify working status and completion
    states = [e.status.state for e in status_updates]
    assert TaskState.TASK_STATE_WORKING in states
    assert TaskState.TASK_STATE_COMPLETED in states


@pytest.mark.asyncio
async def test_executor_thinking_blocks():
    """Verify reasoning tokens emit thoughtBlock A2UI surfaces."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        # 1. Thinking token
        chunk1 = AIMessageChunk(content="")
        setattr(chunk1, "additional_kwargs", {"thinking": "Analyzing pod status"})
        yield ("messages", (chunk1, {"source": "supervisor"}))

        # 2. Text token (should close reasoning)
        yield ("messages", (AIMessageChunk(content="All pods healthy."), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Check pods", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Inspect generated A2UI parts in event queue
    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    a2ui_parts = []
    for ev in status_events:
        msg = getattr(ev.status, "message", None)
        if msg and hasattr(msg, "parts"):
            for p in msg.parts:
                if p.HasField("data"):
                    a2ui_parts.append(p)

    assert len(a2ui_parts) > 0


@pytest.mark.asyncio
async def test_executor_tool_execution_cards():
    """Verify tool call start and completion emit toolExecutionCard surfaces."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        # 1. Tool Call Start
        chunk = AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "kubernetes_get_pods",
                "id": "call_abc",
                "args": '{"namespace": "default"}',
            }],
        )
        yield ("messages", (chunk, {}))

        # 2. Tool Completion
        yield (
            "messages",
            ToolMessage(
                content="pod/nginx-1 Running",
                name="kubernetes_get_pods",
                tool_call_id="call_abc",
                status="success",
            ),
        )

        # 3. Final text
        yield ("messages", (AIMessageChunk(content="Done."), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Get pods", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Verify tool events were processed
    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    assert len(status_events) >= 3


@pytest.mark.asyncio
async def test_executor_hitl_interrupt_and_resume():
    """Verify HITL interrupt sets input_required and resumes with user decision."""
    mock_graph = MagicMock()

    # Step 1: Interrupt execution
    async def mock_astream_interrupt(input_data, config, stream_mode, *args, **kwargs):
        yield (
            "updates",
            {
                "__interrupt__": [
                    MagicMock(
                        value={
                            "action_requests": [
                                {
                                    "action_type": "delete_deployment",
                                    "description": "Delete deployment production-app",
                                    "risk_level": "high",
                                    "args": {"name": "production-app", "namespace": "prod"},
                                }
                            ]
                        }
                    )
                ]
            },
        )

    mock_graph.astream = mock_astream_interrupt
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Delete deployment", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Verify input_required state was emitted
    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_INPUT_REQUIRED in states

    # Step 2: Resume execution with approval
    resumed_command_received = None

    async def mock_astream_resumed(input_data, config, stream_mode, *args, **kwargs):
        nonlocal resumed_command_received
        resumed_command_received = input_data
        yield ("messages", (AIMessageChunk(content="Deployment deleted successfully."), {}))

    mock_graph.astream = mock_astream_resumed

    task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
    ctx_resume = _make_mock_request_context(user_input="approve", task=task)
    event_queue_resume = MockEventQueue()

    await executor.execute(ctx_resume, event_queue_resume)

    assert isinstance(resumed_command_received, Command)
    assert resumed_command_received.resume == "approve"
    states_resume = [e.status.state for e in event_queue_resume.events if hasattr(e, "status")]
    assert TaskState.TASK_STATE_COMPLETED in states_resume


@pytest.mark.asyncio
async def test_executor_ask_user_interrupt():
    """Verify ask_user tool interrupt formats questions and sets input_required."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        yield (
            "updates",
            {
                "__interrupt__": [
                    MagicMock(
                        value={
                            "type": "ask_user",
                            "questions": [
                                {
                                    "question": "Which cluster environment?",
                                    "type": "multiple_choice",
                                    "choices": [{"value": "staging"}, {"value": "production"}],
                                }
                            ],
                            "tool_call_id": "call_ask_1",
                        }
                    )
                ]
            },
        )

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Deploy app", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_INPUT_REQUIRED in states


@pytest.mark.asyncio
async def test_executor_write_todos_plan_surface():
    """Verify write_todos tool call generates planTodoList A2UI surfaces."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        chunk1 = AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "write_todos",
                "id": "call_todos_1",
                "args": json.dumps({"todos": [{"title": "Step 1: Check nodes", "status": "in_progress"}]}),
            }],
        )
        yield ("messages", (chunk1, {}))

        # Update todos
        chunk2 = AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "write_todos",
                "id": "call_todos_2",
                "args": json.dumps({"todos": [{"title": "Step 1: Check nodes", "status": "completed"}]}),
            }],
        )
        yield ("messages", (chunk2, {}))
        yield ("messages", (AIMessageChunk(content="Plan executed."), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Execute plan", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    assert len(status_events) >= 3


@pytest.mark.asyncio
async def test_executor_embedded_a2ui_operations():
    """Verify tool messages with a2ui_operations emit custom A2UI parts."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        obs_payload = {
            "a2ui_operations": [
                {"createSurface": {"surfaceId": "obs-dash-1"}},
            ]
        }
        yield (
            "messages",
            ToolMessage(
                content=json.dumps(obs_payload),
                name="generate_obs_a2ui",
                tool_call_id="call_obs_1",
                status="success",
            ),
        )
        yield ("messages", (AIMessageChunk(content="Dashboard generated."), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Show dashboard", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    assert len(status_events) >= 2


@pytest.mark.asyncio
async def test_executor_cancelled_error():
    """Verify asyncio.CancelledError properly marks task as cancelled."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        raise asyncio.CancelledError()
        yield ("messages", (AIMessageChunk(content="Never reached"), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Run long command", task=task)
    event_queue = MockEventQueue()

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(ctx, event_queue)

    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_CANCELED in states


@pytest.mark.asyncio
async def test_executor_ask_user_interrupt_emits_a2ui():
    """Verify ask_user interrupts construct and emit askUserCard A2UI surfaces."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        yield (
            "updates",
            {
                "__interrupt__": [
                    {
                        "type": "ask_user",
                        "questions": [
                            {"question": "Select target namespace", "type": "text", "required": True}
                        ],
                    }
                ]
            },
        )

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Run diagnostic", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_INPUT_REQUIRED in states

    # Verify A2UI parts in the status message
    input_req_events = [e for e in status_events if e.status.state == TaskState.TASK_STATE_INPUT_REQUIRED]
    assert len(input_req_events) > 0
    parts = input_req_events[0].status.message.parts
    assert len(parts) > 0


@pytest.mark.asyncio
async def test_executor_goal_confirmation_interrupt_emits_a2ui():
    """Verify goal confirmation interrupts construct and emit goalConfirmationCard A2UI surfaces."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, *args, **kwargs):
        yield (
            "updates",
            {
                "__interrupt__": [
                    {
                        "type": "goal_confirmation",
                        "goal": {
                            "goal": "Verify all pods are running healthy",
                            "criteria": ["0 CrashLoopBackOff pods", "All deployments ready"],
                        },
                    }
                ]
            },
        )

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Check goals", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_INPUT_REQUIRED in states


def test_resolve_action_to_query():
    """Verify _resolve_action_to_query for ask_user_response and goal_response."""
    # 1. ask_user_response with answers list
    action = {
        "name": "ask_user_response",
        "value": {
            "status": "answered",
            "answers": ["default", "prod-cluster"],
        },
    }
    resolved = A2AAutoPilotExecutor._resolve_action_to_query(action)
    parsed = json.loads(resolved)
    assert parsed.get("status") == "answered"
    assert parsed.get("answers") == ["default", "prod-cluster"]

    # 2. goal_response
    goal_action = {
        "name": "goal_response",
        "value": {
            "decision": "confirm",
            "goalText": "Verify pods",
        },
    }
    resolved_goal = A2AAutoPilotExecutor._resolve_action_to_query(goal_action)
    parsed_goal = json.loads(resolved_goal)
    assert parsed_goal.get("decision") == "confirm"


@pytest.mark.asyncio
async def test_wrap_resume_with_interrupt_id_mapping():
    """Verify _wrap_resume maps interrupt IDs to structured answer dicts matching OpsCode."""
    mock_graph = MagicMock()
    mock_task = MagicMock()
    int_mock = MagicMock()
    int_mock.id = "int-12345"
    int_mock.value = {
        "type": "ask_user",
        "questions": [{"question": "Primary objective?"}],
    }
    mock_task.interrupts = (int_mock,)

    mock_state = MagicMock()
    mock_state.tasks = (mock_task,)
    mock_graph.aget_state = AsyncMock(return_value=mock_state)

    query = json.dumps({
        "status": "answered",
        "answers": ["Enhancing existing S3 module"],
    })

    task = _make_mock_task()
    config = {"configurable": {"thread_id": "test-ctx"}}

    res = await A2AAutoPilotExecutor._wrap_resume(mock_graph, config, task, query)
    assert isinstance(res, Command)
    assert isinstance(res.resume, dict)
    assert "int-12345" in res.resume
    assert res.resume["int-12345"] == {
        "status": "answered",
        "answers": ["Enhancing existing S3 module"],
    }


@pytest.mark.asyncio
async def test_wrap_resume_with_hitl_interrupt_id_mapping():
    """Verify _wrap_resume maps HITL tool approval interrupts by ID."""
    mock_graph = MagicMock()
    mock_task = MagicMock()
    int_mock = MagicMock()
    int_mock.id = "hitl-999"
    int_mock.value = {
        "type": "hitl",
        "action_requests": [{"tool": "kubectl_apply"}],
    }
    mock_task.interrupts = (int_mock,)

    mock_state = MagicMock()
    mock_state.tasks = (mock_task,)
    mock_graph.aget_state = AsyncMock(return_value=mock_state)

    task = _make_mock_task()
    config = {"configurable": {"thread_id": "test-ctx"}}

    # Test approve
    res = await A2AAutoPilotExecutor._wrap_resume(mock_graph, config, task, "approve")
    assert isinstance(res, Command)
    assert res.resume["hitl-999"] == {"decisions": [{"type": "approve"}]}


def test_extract_query_from_protobuf_datapart_ask_user():
    """Verify extracting direct ask_user answers payload from protobuf DataPart."""
    executor = A2AAutoPilotExecutor(agent=MagicMock())

    val = struct_pb2.Value()
    val.struct_value.update({
        "status": "answered",
        "answers": [
            "Enhancing the existing S3 module (security, replication, lifecycle)",
            "KMS customer-managed keys (CMK) encryption & strict bucket policies",
        ],
    })
    part = Part(data=val)

    ctx = _make_mock_request_context(user_input="", parts=[part])
    query = executor._extract_query(ctx)
    assert query is not None

    parsed = json.loads(query)
    assert parsed["status"] == "answered"
    assert parsed["answers"] == [
        "Enhancing the existing S3 module (security, replication, lifecycle)",
        "KMS customer-managed keys (CMK) encryption & strict bucket policies",
    ]


def test_extract_query_from_protobuf_datapart_nested_user_action():
    """Verify extracting nested userAction payload from protobuf DataPart."""
    executor = A2AAutoPilotExecutor(agent=MagicMock())

    val = struct_pb2.Value()
    val.struct_value.update({
        "userAction": {
            "name": "ask_user_response",
            "value": {
                "status": "answered",
                "answers": ["Option A", "Option B"],
            },
        }
    })
    part = Part(data=val)

    ctx = _make_mock_request_context(user_input="", parts=[part])
    query = executor._extract_query(ctx)
    assert query is not None

    parsed = json.loads(query)
    assert parsed["status"] == "answered"
    assert parsed["answers"] == ["Option A", "Option B"]


@pytest.mark.asyncio
async def test_wrap_resume_ask_user_choice_and_text():
    """Verify _wrap_resume handles single choice and text payload formats."""
    mock_graph = MagicMock()
    mock_task = MagicMock()
    int_mock = MagicMock()
    int_mock.id = "int-single"
    int_mock.value = {
        "type": "ask_user",
        "questions": [{"question": "Preferred region?"}],
    }
    mock_task.interrupts = (int_mock,)
    mock_state = MagicMock()
    mock_state.tasks = (mock_task,)
    mock_graph.aget_state = AsyncMock(return_value=mock_state)

    task = _make_mock_task()
    config = {"configurable": {"thread_id": "test-ctx"}}

    # 1. choice dict
    res_choice = await A2AAutoPilotExecutor._wrap_resume(
        mock_graph, config, task, '{"choice": "us-west-2"}'
    )
    assert isinstance(res_choice, Command)
    assert res_choice.resume["int-single"] == {
        "status": "answered",
        "answers": ["us-west-2"],
    }

    # 2. text dict
    res_text = await A2AAutoPilotExecutor._wrap_resume(
        mock_graph, config, task, '{"text": "custom-cluster-name"}'
    )
    assert isinstance(res_text, Command)
    assert res_text.resume["int-single"] == {
        "status": "answered",
        "answers": ["custom-cluster-name"],
    }

    # 3. cancelled dict
    res_cancel = await A2AAutoPilotExecutor._wrap_resume(
        mock_graph, config, task, '{"status": "cancelled"}'
    )
    assert isinstance(res_cancel, Command)
    assert res_cancel.resume["int-single"] == {
        "status": "cancelled",
        "answers": ["(cancelled)"],
    }


@pytest.mark.asyncio
async def test_executor_ask_user_full_resume_flow():
    """End-to-end test of executing a resume turn from an interactive ask_user prompt."""
    mock_graph = MagicMock()
    mock_task = MagicMock()
    int_mock = MagicMock()
    int_mock.id = "732f84177240e8e481749208b3f3d47a"
    int_mock.value = {
        "type": "ask_user",
        "questions": [
            {"question": "Which architecture?", "type": "multiple_choice"},
            {"question": "Which encryption?", "type": "multiple_choice"},
        ],
    }
    mock_task.interrupts = (int_mock,)
    mock_state = MagicMock()
    mock_state.tasks = (mock_task,)
    mock_graph.aget_state = AsyncMock(return_value=mock_state)

    resumed_command_received = None

    async def mock_astream_resumed(input_data, config, stream_mode, *args, **kwargs):
        nonlocal resumed_command_received
        resumed_command_received = input_data
        yield ("messages", (AIMessageChunk(content="Setting up AWS EKS cluster now..."), {}))

    mock_graph.astream = mock_astream_resumed
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)

    task = _make_mock_task(state=TaskState.TASK_STATE_INPUT_REQUIRED)

    # Build DataPart from user's submitted answers
    val = struct_pb2.Value()
    val.struct_value.update({
        "status": "answered",
        "answers": [
            "Enhancing the existing S3 module (security, replication, lifecycle)",
            "KMS customer-managed keys (CMK) encryption & strict bucket policies",
        ],
    })
    part = Part(data=val)

    ctx = _make_mock_request_context(user_input="", parts=[part], task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Assert graph received canonical OpsCode resume command
    assert isinstance(resumed_command_received, Command)
    assert isinstance(resumed_command_received.resume, dict)
    assert "732f84177240e8e481749208b3f3d47a" in resumed_command_received.resume
    assert resumed_command_received.resume["732f84177240e8e481749208b3f3d47a"] == {
        "status": "answered",
        "answers": [
            "Enhancing the existing S3 module (security, replication, lifecycle)",
            "KMS customer-managed keys (CMK) encryption & strict bucket policies",
        ],
    }

    # Assert status events emitted WORKING -> COMPLETED
    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_COMPLETED in states


@pytest.mark.asyncio
async def test_handle_interrupt_event_goal_review_ui():
    """Verify _handle_interrupt_event produces goalConfirmationCard A2UI surface for goal_review."""
    executor = A2AAutoPilotExecutor(agent=MagicMock())
    updater = MagicMock()
    updater.update_status = AsyncMock()
    renderer = _StreamRenderer(updater, "ctx-goal-1", "task-goal-1")

    interrupt_val = {
        "type": "goal_review",
        "objective": "Deploy ArgoCD and configure apps",
        "criteria": [
            "Install ArgoCD helm chart",
            "Verify server pod is Running",
            "Sync guestbook application",
        ],
    }

    await executor._handle_interrupt_event(
        interrupt_val=interrupt_val,
        use_ui=True,
        renderer=renderer,
        context_id="ctx-goal-1",
        task_id="task-goal-1",
        updater=updater,
        run_id="run-goal-1",
        step_index=1,
    )

    updater.update_status.assert_called_once()
    args, _ = updater.update_status.call_args
    assert args[0] == TaskState.TASK_STATE_INPUT_REQUIRED
    msg = args[1]
    assert len(msg.parts) > 0


@pytest.mark.asyncio
async def test_wrap_resume_goal_review():
    """Verify _wrap_resume maps goal_review interrupt ID to parsed user decision."""
    mock_graph = MagicMock()
    mock_task = MagicMock()
    int_mock = MagicMock()
    int_mock.id = "goal_int_999"
    int_mock.value = {
        "type": "goal_review",
        "objective": "Setup Prometheus",
        "criteria": ["Install Prometheus", "Verify metrics"],
    }
    mock_task.interrupts = (int_mock,)
    mock_state = MagicMock()
    mock_state.tasks = (mock_task,)
    mock_graph.aget_state = AsyncMock(return_value=mock_state)

    task = _make_mock_task(state=TaskState.TASK_STATE_INPUT_REQUIRED)
    config = {"configurable": {"thread_id": "test-ctx"}}

    # User submits edit decision
    user_decision = {
        "decision": "edit",
        "criteria": ["Install Prometheus stack", "Verify scrape targets"],
    }
    query = json.dumps(user_decision)
    cmd = await A2AAutoPilotExecutor._wrap_resume(mock_graph, config, task, query)

    assert isinstance(cmd, Command)
    assert isinstance(cmd.resume, dict)
    assert "goal_int_999" in cmd.resume

@pytest.mark.asyncio
async def test_stream_agent_handles_model_404_error_gracefully():
    """Verify _stream_agent catches model 404 error, emits friendly message and TASK_STATE_FAILED."""
    executor = A2AAutoPilotExecutor()
    updater = MagicMock()
    updater.update_status = AsyncMock()
    updater.complete = AsyncMock()
    event_queue = MockEventQueue()
    task = _make_mock_task()

    mock_graph = MagicMock()

    async def _failing_stream(*args, **kwargs):
        raise RuntimeError("Publisher model `gemini-3.1-pro` was not found (404)")
        yield  # make it an async generator

    mock_graph.astream = _failing_stream

    await executor._stream_agent(
        query="hi",
        task=task,
        updater=updater,
        event_queue=event_queue,
        context_id="ctx-error-1",
        use_ui=True,
        requested_model="gemini-3.1-pro",
        agent_graph=mock_graph,
        config={"configurable": {"thread_id": "ctx-error-1"}},
    )

    updater.update_status.assert_called_once()
    state, msg = updater.update_status.call_args[0]
    assert state == TaskState.TASK_STATE_FAILED
    assert "unavailable in your project/region" in msg.parts[0].text
    updater.complete.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_task_creates_new_task_if_current_terminal():
    """Verify _resolve_task spawns a new task if the context.current_task is already completed/failed."""
    event_queue = MockEventQueue()
    failed_task = _make_mock_task(state=TaskState.TASK_STATE_FAILED, task_id="old-failed-task")
    req_context = _make_mock_request_context(user_input="new turn message", task=failed_task)

    new_task = await A2AAutoPilotExecutor._resolve_task(req_context, event_queue)
    assert new_task.id != "old-failed-task"
    assert len(event_queue.events) == 1
    assert event_queue.events[0].id == new_task.id


def test_ensure_agent_preserves_checkpointer_across_model_changes():
    """Verify changing models in executor reuses the same MemorySaver instance so thread state is preserved."""
    executor = A2AAutoPilotExecutor()
    with patch("k8s_autopilot.agent.create_k8s_autopilot_agent") as mock_create:
        mock_create.side_effect = lambda **kwargs: (MagicMock(), MagicMock())
        agent1 = executor._ensure_agent(requested_model="gemini-3.7-flash")
        checkpointer1 = executor.checkpointer
        assert checkpointer1 is not None

        # Change to a different model
        agent2 = executor._ensure_agent(requested_model="gemini-2.5-flash")
        checkpointer2 = executor.checkpointer

        assert checkpointer1 is checkpointer2
        assert executor._active_model == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_thread_memory_retention_across_model_switches():
    """Verify that thread history is completely preserved when switching models mid-conversation."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    executor = A2AAutoPilotExecutor(checkpointer=checkpointer)

    thread_id = "test-thread-continuity-123"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    from langgraph.checkpoint.base import empty_checkpoint

    # Turn 1: Save state in checkpointer
    initial_checkpoint = empty_checkpoint()
    initial_checkpoint["channel_values"]["messages"] = [
        HumanMessage(content="hi how are you"),
        AIMessage(content="Hello Sandeep! I am doing well."),
    ]
    initial_checkpoint["channel_values"]["_context_tokens"] = 42
    initial_checkpoint["channel_versions"]["messages"] = 1
    initial_checkpoint["channel_versions"]["_context_tokens"] = 1
    await checkpointer.aput(
        config,
        initial_checkpoint,
        {},
        {"messages": 1, "_context_tokens": 1},
    )

    # Verify checkpointer has 2 messages
    loaded = await checkpointer.aget_tuple(config)
    assert loaded is not None
    assert len(loaded.checkpoint["channel_values"]["messages"]) == 2

    # Now switch to a different model in executor
    with patch("k8s_autopilot.agent.create_k8s_autopilot_agent") as mock_create:
        mock_create.side_effect = lambda **kwargs: (MagicMock(), MagicMock())
        executor._ensure_agent(requested_model="gemini-2.5-flash")

    # State in checkpointer for that thread is completely intact and shared
    loaded_after_switch = await executor.checkpointer.aget_tuple(config)
    assert loaded_after_switch is not None
    msgs = loaded_after_switch.checkpoint["channel_values"]["messages"]
    assert len(msgs) == 2
    assert msgs[0].content == "hi how are you"
    assert msgs[1].content == "Hello Sandeep! I am doing well."


@pytest.mark.asyncio
async def test_executor_subgraphs_subagent_events():
    """Verify subgraphs=True yields custom subagent events and serializes metadata.subagent."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, subgraphs=False):
        assert subgraphs is True
        assert config.get("configurable", {}).get("approval_mode") is not None
        # Emit subagent start event
        start_payload = {
            "type": "subagent",
            "phase": "start",
            "id": "call_sub_123",
            "subagent_type": "helm-operator",
            "description": "List all Helm releases",
            "label": "helm-operator: List all Helm releases",
        }
        yield (("task:call_sub_123",), "custom", start_payload)

        # Child subagent internal message (should be isolated)
        yield (("task:call_sub_123",), "messages", (AIMessageChunk(content="Subagent internal log"), {}))

        # Emit subagent complete event
        complete_payload = {
            "type": "subagent",
            "phase": "complete",
            "id": "call_sub_123",
            "duration_ms": 1200,
        }
        yield (("task:call_sub_123",), "custom", complete_payload)

        # Main agent response
        yield ((), "messages", (AIMessageChunk(content="Found 3 Helm releases."), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="List releases", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Find status events containing subagent metadata
    subagent_events = []
    for ev in event_queue.events:
        msg = getattr(getattr(ev, "status", None), "message", None)
        if msg and "subagent" in msg.metadata:
            subagent_events.append(json.loads(msg.metadata["subagent"]))

    assert len(subagent_events) == 2
    assert subagent_events[0]["phase"] == "start"
    assert subagent_events[0]["subagent_type"] == "helm-operator"
    assert subagent_events[1]["phase"] == "complete"
    assert subagent_events[1]["duration_ms"] == 1200


@pytest.mark.asyncio
async def test_executor_subgraphs_message_isolation():
    """Verify child subagent token chunks are isolated and do not pollute main chat."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, subgraphs=False):
        # Child subgraph token
        yield (("task:sub_1",), "messages", (AIMessageChunk(content="INNER_CHILD_TOKEN"), {}))
        # Main supervisor token
        yield ((), "messages", (AIMessageChunk(content="MAIN_SUPERVISOR_TOKEN"), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Test isolation", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    accumulated_texts = []
    for ev in event_queue.events:
        msg = getattr(getattr(ev, "status", None), "message", None)
        if msg and msg.parts:
            for p in msg.parts:
                if p.HasField("text") and p.text:
                    accumulated_texts.append(p.text)

    all_text = " ".join(accumulated_texts)
    assert "MAIN_SUPERVISOR_TOKEN" in all_text
    assert "INNER_CHILD_TOKEN" not in all_text


@pytest.mark.asyncio
async def test_executor_subagent_messages_chunk_forwarding():
    """Verify child subagent thinking and tool calls generate A2UI surfaces without polluting chat text."""
    mock_graph = MagicMock()

    async def mock_astream(input_data, config, stream_mode, subgraphs=False):
        # 1. Subagent thinking
        chunk_thinking = AIMessageChunk(content="")
        setattr(chunk_thinking, "additional_kwargs", {"thinking": "Analyzing helm charts..."})
        yield (("tools:task:call_sub1",), "messages", (chunk_thinking, {"subagent_type": "helm-operator"}))

        # 2. Subagent tool call (helm_list)
        chunk_tool = AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "helm_list",
                "id": "call_hl_1",
                "args": json.dumps({"all_namespaces": True}),
            }],
        )
        yield (("tools:task:call_sub1",), "messages", (chunk_tool, {"subagent_type": "helm-operator"}))

        # 3. Subagent tool completion
        yield (
            ("tools:task:call_sub1",),
            "messages",
            (
                ToolMessage(
                    content="nginx-ingress deployed 1.0.0",
                    name="helm_list",
                    tool_call_id="call_hl_1",
                    status="success",
                ),
                {"subagent_type": "helm-operator"},
            ),
        )

        # 4. Subagent internal summary text (must not appear in chat)
        yield (("tools:task:call_sub1",), "messages", (AIMessageChunk(content="Internal subagent summary"), {}))

        # 5. Main supervisor response text
        yield ((), "messages", (AIMessageChunk(content="Here are your Helm releases:"), {}))

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="List helm releases", task=task)
    event_queue = MockEventQueue()

    await executor.execute(ctx, event_queue)

    # Collect all A2UI operations and texts
    a2ui_ops = []
    accumulated_texts = []
    for ev in event_queue.events:
        msg = getattr(getattr(ev, "status", None), "message", None)
        if msg and msg.parts:
            for p in msg.parts:
                if p.HasField("data"):
                    a2ui_ops.append(p)
                if p.HasField("text") and p.text:
                    accumulated_texts.append(p.text)

    # Verify A2UI surfaces were emitted for subagent thinking and tools
    assert len(a2ui_ops) >= 2

    # Verify main text does NOT contain internal subagent summary
    all_text = " ".join(accumulated_texts)
    assert "Here are your Helm releases:" in all_text
    assert "Internal subagent summary" not in all_text


@pytest.mark.asyncio
async def test_executor_interrupt_no_generator_exit():
    """Verify stream finishes iterating naturally on interrupt without throwing GeneratorExit."""
    mock_graph = MagicMock()
    generator_completed_naturally = False

    async def mock_astream(input_data, config, stream_mode, subgraphs=False):
        nonlocal generator_completed_naturally
        try:
            # Emit interrupt update
            yield (
                (),
                "updates",
                {
                    "__interrupt__": [
                        {
                            "id": "int_test_1",
                            "value": {
                                "type": "hitl",
                                "action_requests": [{"name": "helm_install", "args": {}}],
                            },
                        }
                    ]
                },
            )
            # Emit post-interrupt LangGraph step update (must be consumed cleanly)
            yield ((), "updates", {"_context_tokens": 150})
            generator_completed_naturally = True
        except GeneratorExit:
            # If consumer breaks out of loop, Python calls aclose() which raises GeneratorExit here
            raise

    mock_graph.astream = mock_astream
    mock_graph.name = "k8sAutopilotAgent"

    executor = A2AAutoPilotExecutor(agent=mock_graph)
    task = _make_mock_task()
    ctx = _make_mock_request_context(user_input="Deploy chart", task=task)
    event_queue = MockEventQueue()

    # Must complete without GeneratorExit
    await executor.execute(ctx, event_queue)

    assert generator_completed_naturally is True
    # Verify input_required state was emitted to event queue
    status_events = [e for e in event_queue.events if hasattr(e, "status")]
    states = [e.status.state for e in status_events]
    assert TaskState.TASK_STATE_INPUT_REQUIRED in states
