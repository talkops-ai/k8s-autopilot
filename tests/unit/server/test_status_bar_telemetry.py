"""Unit tests for status bar backend enhancements and thread telemetry."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from starlette.applications import Starlette
from starlette.testclient import TestClient

from k8s_autopilot.agent.factory import _should_interrupt_tool_call
from k8s_autopilot.api.models import ThreadCreate, ThreadTelemetryResponse
from k8s_autopilot.api.routes import create_thread_routes
from k8s_autopilot.api.service import ThreadService, set_thread_service
from k8s_autopilot.api.settings_routes import create_settings_routes, set_config_store
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.security.approval_mode import ApprovalMode
from k8s_autopilot.server.executor import A2AAutoPilotExecutor


# ---------------------------------------------------------------------------
# 1. Tests for _should_interrupt_tool_call
# ---------------------------------------------------------------------------

class TestShouldInterruptToolCall:
    @pytest.fixture(autouse=True)
    def reset_mode(self):
        orig = get_settings().approval_mode
        yield
        get_settings().approval_mode = orig or "manual"

    def test_manual_mode_requires_interrupt(self):
        get_settings().approval_mode = "manual"
        req = {"action": {"name": "kubectl_delete", "args": {"name": "pod-1"}}}
        assert _should_interrupt_tool_call(req) is True

    def test_auto_mode_does_not_interrupt(self):
        get_settings().approval_mode = "auto"
        req = {"action": {"name": "kubectl_delete", "args": {"name": "pod-1"}}}
        assert _should_interrupt_tool_call(req) is False

    def test_yolo_mode_does_not_interrupt(self):
        get_settings().approval_mode = "yolo"
        req = {"action": {"name": "kubectl_delete", "args": {"name": "pod-1"}}}
        assert _should_interrupt_tool_call(req) is False

    def test_state_override_auto_mode(self):
        get_settings().approval_mode = "manual"
        req = {
            "state": {"approval_mode": "auto"},
            "action": {"name": "kubectl_delete", "args": {}},
        }
        assert _should_interrupt_tool_call(req) is False

    def test_agents_md_write_never_interrupts(self):
        get_settings().approval_mode = "manual"
        req = {
            "action": {
                "name": "write_to_file",
                "args": {"TargetFile": "/path/to/AGENTS.md", "content": "# Agent memory"},
            }
        }
        assert _should_interrupt_tool_call(req) is False


# ---------------------------------------------------------------------------
# 2. Tests for _wrap_resume HITL choices
# ---------------------------------------------------------------------------

class TestWrapResumeChoices:
    @pytest.mark.asyncio
    async def test_auto_approve_all_persists_auto_mode(self):
        mock_graph = MagicMock()
        mock_task = MagicMock()
        int_mock = MagicMock()
        int_mock.id = "hitl-101"
        int_mock.value = {
            "type": "hitl",
            "action_requests": [{"tool": "kubectl_apply"}],
        }
        mock_task.interrupts = (int_mock,)
        mock_state = MagicMock()
        mock_state.tasks = (mock_task,)
        mock_graph.aget_state = AsyncMock(return_value=mock_state)
        mock_graph.store = AsyncMock()
        mock_graph.store.aput = AsyncMock()

        task = Task(id="task-101", context_id="thread-auto-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED))
        config = {"configurable": {"thread_id": "thread-auto-1"}}

        # Send auto_approve_all decision
        query = json.dumps({"decision": "auto_approve_all"})
        cmd = await A2AAutoPilotExecutor._wrap_resume(mock_graph, config, task, query)

        assert isinstance(cmd, Command)
        assert isinstance(cmd.resume, dict)
        assert cmd.resume["hitl-101"] == {"decisions": [{"type": "approve"}]}
        assert get_settings().approval_mode == "auto"

    @pytest.mark.asyncio
    async def test_reject_with_message(self):
        mock_graph = MagicMock()
        mock_task = MagicMock()
        int_mock = MagicMock()
        int_mock.id = "hitl-102"
        int_mock.value = {
            "type": "hitl",
            "action_requests": [{"tool": "kubectl_delete"}],
        }
        mock_task.interrupts = (int_mock,)
        mock_state = MagicMock()
        mock_state.tasks = (mock_task,)
        mock_graph.aget_state = AsyncMock(return_value=mock_state)

        task = Task(id="task-102", context_id="thread-reject-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED))
        config = {"configurable": {"thread_id": "thread-reject-1"}}

        # Send reject with custom feedback message
        query = json.dumps({"decision": "reject", "message": "Do not delete default namespace pods"})
        cmd = await A2AAutoPilotExecutor._wrap_resume(mock_graph, config, task, query)

        assert isinstance(cmd, Command)
        assert isinstance(cmd.resume, dict)
        assert cmd.resume["hitl-102"] == {
            "decisions": [{"type": "reject", "message": "Do not delete default namespace pods"}]
        }


# ---------------------------------------------------------------------------
# 3. Tests for Trace Metadata Attachment
# ---------------------------------------------------------------------------

class TestTraceMetadata:
    def test_attach_trace_metadata_payload(self):
        executor = A2AAutoPilotExecutor(agent=MagicMock())
        msg = Message(role=Role.ROLE_AGENT, parts=[Part(text="Analyzing...")])

        executor._attach_trace_metadata(
            msg,
            run_id="run-test-123",
            step_index=4,
            event_type="tool_result",
            approval_mode="auto",
            goal_status="active",
            rubric_active=True,
            rubric_label="✓ Rubric set",
            input_tokens=1500,
            output_tokens=500,
            cost_usd=0.0125,
            model="google_genai:gemini-3.7-flash",
            reasoning_effort="high",
        )

        assert msg.metadata["traceRunId"] == "run-test-123"
        assert msg.metadata["traceStepIndex"] == 4
        assert msg.metadata["eventType"] == "tool_result"
        assert msg.metadata["approval_mode"] == "auto"
        assert int(float(str(msg.metadata["total_tokens"]))) == 2000

        # Verify structured trace JSON
        trace_str = str(msg.metadata["trace"])
        trace_json = json.loads(trace_str)
        assert trace_json["run_id"] == "run-test-123"
        assert trace_json["step_index"] == 4
        assert trace_json["approval_mode"] == "auto"
        assert trace_json["goal_status"] == "active"
        assert trace_json["rubric_active"] is True
        assert trace_json["rubric_label"] == "✓ Rubric set"
        assert trace_json["input_tokens"] == 1500
        assert trace_json["output_tokens"] == 500
        assert trace_json["total_tokens"] == 2000
        assert trace_json["cost_usd"] == 0.0125
        assert trace_json["model"] == "google_genai:gemini-3.7-flash"
        assert trace_json["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# 4. Tests for Telemetry Service & Endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_thread_telemetry_service():
    checkpointer = MemorySaver()
    service = ThreadService(checkpointer=checkpointer)

    thread_id = str(uuid.uuid4())
    await service.auto_touch(thread_id, user_query="Diagnose cluster alerts")

    telemetry = await service.get_thread_telemetry(thread_id)
    assert telemetry is not None
    assert str(telemetry.thread_id) == thread_id
    assert telemetry.approval_mode in ("manual", "auto", "yolo")
    assert telemetry.model.spec is not None
    assert telemetry.usage.total_tokens >= 0
    assert isinstance(telemetry.subagents, list)


@pytest.mark.asyncio
async def test_telemetry_endpoint_http():
    checkpointer = MemorySaver()
    service = ThreadService(checkpointer=checkpointer)
    set_thread_service(service)

    thread_id = str(uuid.uuid4())
    await service.auto_touch(thread_id, user_query="Diagnose pod failures")

    routes = create_thread_routes()
    app = Starlette(routes=routes)
    client = TestClient(app)

    resp = client.get(f"/threads/{thread_id}/telemetry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thread_id"] == thread_id
    assert "approval_mode" in data
    assert "goal" in data
    assert "usage" in data
    assert "model" in data
    assert "subagents" in data
    assert data["usage"]["total_tokens"] == data["usage"]["input_tokens"] + data["usage"]["output_tokens"]


# ---------------------------------------------------------------------------
# 5. Tests for Approval Mode API Endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
async def settings_app_client(tmp_path):
    adapter = SqliteConfigAdapter(tmp_path / "test_settings_api.db")
    store = ConfigStore(adapter)
    await store.initialize()
    await set_config_store(store)

    routes = create_settings_routes()
    app = Starlette(routes=routes)
    return TestClient(app)


def test_get_and_post_approval_mode_api(settings_app_client):
    # 1. GET initial approval mode
    resp = settings_app_client.get("/api/settings/approval-mode")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["approval_mode"] in ("manual", "auto", "yolo")

    # 2. POST change to "auto"
    post_resp = settings_app_client.post(
        "/api/settings/approval-mode",
        json={"mode": "auto"},
    )
    assert post_resp.status_code == 200
    post_data = post_resp.json()
    assert post_data["status"] == "success"
    assert post_data["approval_mode"] == "auto"

    # 3. GET verifies updated mode
    get_resp = settings_app_client.get("/api/settings/approval-mode")
    assert get_resp.status_code == 200
    assert get_resp.json()["approval_mode"] == "auto"

    # 4. POST invalid mode returns 400
    bad_resp = settings_app_client.post(
        "/api/settings/approval-mode",
        json={"mode": "invalid_mode_xyz"},
    )
    assert bad_resp.status_code == 400


# ---------------------------------------------------------------------------
# 5. Tests for Goal Confirmation Stream Message & Telemetry Persistence
# ---------------------------------------------------------------------------


class TestGoalConfirmationAndTelemetry:
    @pytest.mark.asyncio
    async def test_propose_goal_emits_visible_stream_message(self):
        """Verify propose_goal ToolMessage emits a visible chat message to the stream."""
        from langchain_core.messages import ToolMessage
        from a2a.types import Task, TaskStatus, TaskState
        from unittest.mock import MagicMock, AsyncMock

        executor = A2AAutoPilotExecutor()
        task = Task(id="task-goal-1", context_id="thread-goal-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        updater = MagicMock()
        updater.update_status = AsyncMock()

        ctx = MagicMock()
        ctx.use_ui = True
        ctx.context_id = "thread-goal-1"
        ctx.task = task
        ctx.updater = updater
        ctx.renderer = MagicMock()
        ctx.renderer.message_id = "msg-1"
        ctx.telemetry = MagicMock()
        ctx.telemetry.run_id = "run-1"
        ctx.telemetry.step_index = 0
        ctx.telemetry.current_approval_mode = "manual"
        ctx.telemetry.current_goal_status = "active"
        ctx.telemetry.current_goal_objective = "Deploy ArgoCD"
        ctx.telemetry.current_rubric = "- Check namespace"
        ctx.telemetry.derive_rubric_label.return_value = "✓ Rubric set"
        ctx.telemetry.cumulative_input_tokens = 100
        ctx.telemetry.cumulative_output_tokens = 50
        ctx.telemetry.cumulative_cost_usd = 0.01
        ctx.telemetry.active_model = "gemini-3.7-flash"
        ctx.telemetry.active_effort = "medium"

        tool_content = (
            "Goal confirmed by user.\n"
            "**Objective:** Deploy ArgoCD\n"
            "**Criteria:**\n- Check namespace"
        )
        tool_msg = ToolMessage(
            content=tool_content,
            name="propose_goal",
            tool_call_id="call-goal-123",
        )

        await executor._handle_tool_message(tool_msg, ctx)

        # Verify telemetry status update was emitted to updater
        # without intermediate chat text.
        assert updater.update_status.called
        call_args = updater.update_status.call_args[0]
        assert call_args[0] == TaskState.TASK_STATE_WORKING
        emitted_msg = call_args[1]
        assert len(emitted_msg.parts) == 0
        assert emitted_msg.metadata["eventType"] == "goal_confirmed"
        assert emitted_msg.metadata["goal_objective"] == "Deploy ArgoCD"
        assert emitted_msg.metadata["rubric_label"] == "✓ Rubric set"

    @pytest.mark.asyncio
    async def test_init_stream_telemetry_restores_goal_from_snapshot(self):
        """Verify _init_stream_telemetry restores goal and rubric from graph snapshot."""
        from unittest.mock import MagicMock, AsyncMock

        executor = A2AAutoPilotExecutor()
        mock_graph = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.values = {
            "_goal_status": "active",
            "_goal_objective": "Configure Prometheus",
            "_sticky_rubric": "- Verify scrape targets\n- Check alert rules",
        }
        mock_graph.aget_state = AsyncMock(return_value=mock_snapshot)

        t = await executor._init_stream_telemetry(
            mock_graph,
            context_id="thread-turn2",
            active_model="gemini-3.7-flash",
            active_effort="medium",
        )

        assert t.current_goal_status == "active"
        assert t.current_goal_objective == "Configure Prometheus"
        assert t.current_rubric == "- Verify scrape targets\n- Check alert rules"
        assert t.derive_rubric_label() == "✓ Rubric set"

    @pytest.mark.asyncio
    async def test_interrupt_event_propagates_goal_and_rubric(self):
        """Verify _handle_interrupt_event preserves goal_objective and goal_rubric in trace metadata."""
        from unittest.mock import MagicMock, AsyncMock
        from a2a.types import TaskState

        executor = A2AAutoPilotExecutor()
        updater = MagicMock()
        updater.update_status = AsyncMock()
        renderer = MagicMock()
        renderer.message_id = "msg-int-1"

        raw_interrupt = {
            "action_requests": [{"tool_name": "kubectl_delete", "description": "Delete pods"}],
            "phase": "execution",
        }

        await executor._handle_interrupt_event(
            interrupt_val=raw_interrupt,
            use_ui=False,
            renderer=renderer,
            updater=updater,
            context_id="thread-hitl",
            task_id="task-hitl",
            run_id="run-1",
            step_index=2,
            approval_mode="manual",
            goal_status="active",
            goal_objective="Clean cluster",
            goal_rubric="- Remove dead pods",
            rubric_active=True,
            rubric_label="✓ Rubric set",
        )

        assert updater.update_status.called
        call_args = updater.update_status.call_args[0]
        assert call_args[0] == TaskState.TASK_STATE_INPUT_REQUIRED
        msg = call_args[1]
        assert msg.metadata["goal_objective"] == "Clean cluster"
        assert msg.metadata["goal_rubric"] == "- Remove dead pods"
        assert msg.metadata["rubric_label"] == "✓ Rubric set"

    @pytest.mark.asyncio
    async def test_get_thread_state_filters_conversation_control_messages(self):
        """Verify get_thread_state strips goal control notices from returned messages."""
        from unittest.mock import AsyncMock, patch
        from langchain_core.messages import AIMessage, HumanMessage
        from k8s_autopilot.api.service import ThreadService

        human_msg = HumanMessage(content="Delete test pods", id="msg-h1")
        control_msg = HumanMessage(
            content="[K8s Autopilot] Goal/rubric state changed.\n\n- Goal status: active",
            id="msg-ctrl-1",
        )
        ai_msg = AIMessage(content="Deleted test pods successfully.", id="msg-a1")

        mock_cp = MagicMock()
        service = ThreadService(checkpointer=mock_cp)
        with patch(
            "k8s_autopilot.state.session.SessionManager.get_thread_messages",
            new_callable=AsyncMock,
            return_value=[human_msg, control_msg, ai_msg],
        ):
            resp = await service.get_thread_state("thread-123")
            assert resp is not None
            # Only human_msg and ai_msg should be present; control_msg must be filtered
            assert len(resp.messages) == 2
            assert resp.messages[0]["content"] == "Delete test pods"
            assert resp.messages[1]["content"] == "Deleted test pods successfully."


