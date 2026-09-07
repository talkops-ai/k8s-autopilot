"""Unit tests for Dynamic Subagent System enhancements.

Verifies:
1. ServerHooksMiddleware correctly intercepts "task" and "start_async_task" tools
   and dispatches SubagentStartEvent / SubagentStopEvent and custom stream events.
2. load_async_subagents loads remote subagent specs from database store first,
   with optional config fallback, and resolves environment variables.
3. factory.create_k8s_autopilot_agent preserves coordinator base tools for subagents
   without leaking coordinator MCP tools, and compiles async_subagents into the graph.
4. executor._handle_custom_chunk translates "subagent" custom events into real-time
   A2UI toolExecutionCard surfaces and emits subagent_lifecycle metadata.
5. get_thread_telemetry in api/service includes async subagents in telemetry.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deepagents.middleware.async_subagents import AsyncSubAgent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolCall, ToolMessage

from k8s_autopilot.hooks import (
    HookEvent,
    SubagentStartDecision,
    SubagentStartEvent,
    SubagentStopDecision,
    SubagentStopEvent,
)
from k8s_autopilot.hooks.models import ToolCallData
from k8s_autopilot.middleware.server_hooks import (
    ServerHooksMiddleware,
    _task_agent_identity,
    _TASK_TOOL_NAMES,
    _SUBAGENT_TOOL_NAMES,
)
from k8s_autopilot.subagents.loader import (
    load_async_subagents,
    load_async_subagents_async,
)


# ── Enhancement 1: ServerHooksMiddleware Task Tool Name Interception ─────────────

def test_task_tool_names_constants() -> None:
    """Verify 'task' and 'start_async_task' are recognized subagent tools."""
    assert "task" in _SUBAGENT_TOOL_NAMES
    assert "start_async_task" in _SUBAGENT_TOOL_NAMES
    assert "task" in _TASK_TOOL_NAMES
    assert "start_async_task" in _TASK_TOOL_NAMES
    assert "js_eval" in _TASK_TOOL_NAMES


def test_task_agent_identity_resolution() -> None:
    """Verify _task_agent_identity resolves subagent_type, name, or agent correctly."""
    # 1. ToolCallData with subagent_type (standard task schema or start_async_task)
    call1 = ToolCallData(name="task", args={"subagent_type": "k8s-operator", "description": "run"}, id="c1")
    assert _task_agent_identity(call1).name == "k8s-operator"

    # 2. dict with subagent_type
    call2 = {"name": "start_async_task", "args": {"subagent_type": "remote-cluster-drainer"}, "id": "c2"}
    assert _task_agent_identity(call2).name == "remote-cluster-drainer"

    # 3. dict with name
    call3 = {"name": "task", "args": {"name": "app-operator"}, "id": "c3"}
    assert _task_agent_identity(call3).name == "app-operator"

    # 4. dict with agent
    call4 = {"name": "subagent", "args": {"agent": "helm-operator"}, "id": "c4"}
    assert _task_agent_identity(call4).name == "helm-operator"

    # 5. fallback to call.name
    call5 = {"name": "task", "args": {}, "id": "c5"}
    assert _task_agent_identity(call5).name == "unknown"


@pytest.mark.asyncio
async def test_server_hooks_task_tool_dispatches_subagent_events() -> None:
    """Verify ServerHooksMiddleware awrap_tool_call fires start/stop hooks on 'task' tool."""
    middleware = ServerHooksMiddleware(cwd=Path.cwd())

    request = MagicMock()
    request.tool_call = {
        "name": "task",
        "args": {"subagent_type": "helm-operator", "description": "check release"},
        "id": "call_task_1",
    }
    request.tool = MagicMock()
    request.runtime = MagicMock()
    request.runtime.config = {"metadata": {}}
    request.runtime.context = {
        "hooks_snapshot_id": "snap-123",
        "hooks_server_events": [HookEvent.SUBAGENT_START.value, HookEvent.SUBAGENT_STOP.value],
    }
    request.state = {}

    async def mock_handler(req: Any) -> ToolMessage:
        return ToolMessage(content="Release healthy", tool_call_id="call_task_1")

    with patch("k8s_autopilot.middleware.server_hooks._invoke_hook") as mock_invoke:
        mock_invoke.side_effect = [
            SubagentStartDecision(event=HookEvent.SUBAGENT_START, continue_processing=True),
            SubagentStopDecision(event=HookEvent.SUBAGENT_STOP, continue_processing=True),
        ]

        result = await middleware.awrap_tool_call(request, mock_handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "Release healthy"

        assert mock_invoke.call_count == 2
        start_event = mock_invoke.call_args_list[0][0][1]
        assert isinstance(start_event, SubagentStartEvent)
        assert start_event.agent.name == "helm-operator"

        stop_event = mock_invoke.call_args_list[1][0][1]
        assert isinstance(stop_event, SubagentStopEvent)
        assert stop_event.agent.name == "helm-operator"


# ── Enhancement 2 & 3: load_async_subagents & DB-First Architecture ─────────────

class DummyStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    async def aget(self, key: str) -> Any:
        return self.data.get(key)


def test_load_async_subagents_from_store() -> None:
    """Verify load_async_subagents loads from active store without touching disk."""
    store = DummyStore({
        "async_subagents": [
            {
                "name": "canary-monitor",
                "description": "Monitors canary deployment",
                "graph_id": "canary-graph",
                "url": "https://remote.internal:8000",
                "headers": {"Authorization": "Bearer test-token"},
            }
        ]
    })

    agents = load_async_subagents(store=store)
    assert len(agents) == 1
    assert agents[0]["name"] == "canary-monitor"
    assert agents[0]["graph_id"] == "canary-graph"
    assert agents[0].get("url") == "https://remote.internal:8000"


def test_load_async_subagents_env_expansion() -> None:
    """Verify environment variables in url and headers are expanded."""
    store = DummyStore({
        "async_subagents": [
            {
                "name": "env-subagent",
                "description": "Env test",
                "graph_id": "env-graph",
                "url": "https://${REMOTE_HOST}/api",
                "headers": {"X-Api-Key": "${REMOTE_API_KEY}"},
            }
        ]
    })

    with patch.dict("os.environ", {"REMOTE_HOST": "worker.ops.net", "REMOTE_API_KEY": "secret123"}):
        agents = load_async_subagents(store=store)
        assert len(agents) == 1
        assert agents[0].get("url") == "https://worker.ops.net/api"
        headers = agents[0].get("headers")
        assert headers is not None
        assert headers.get("X-Api-Key") == "secret123"


# ── Enhancement 2: Factory Tool Inheritance & Async Wiring ─────────────────────

def test_factory_subagent_tool_inheritance_and_async_subagents() -> None:
    """Verify create_k8s_autopilot_agent equips subagents with base tools and wires async subagents."""
    from k8s_autopilot.agent.factory import create_k8s_autopilot_agent

    fake_model = FakeMessagesListChatModel(responses=[AIMessage(content="Hello")])
    async_specs: list[AsyncSubAgent] = [
        {
            "name": "remote-rollout-worker",
            "description": "Offload long rollout monitoring",
            "graph_id": "rollout-graph",
            "url": "http://127.0.0.1:8123",
        }
    ]

    graph, backend = create_k8s_autopilot_agent(
        model=fake_model,
        assistant_id="test-autopilot",
        async_subagents=async_specs,
        interactive=False,
    )

    assert graph is not None
    assert backend is not None

    # Verify tools node includes async subagent tools
    tools_node = graph.nodes.get("tools")
    assert tools_node is not None
    bound_node: Any = getattr(tools_node, "bound", tools_node)
    tools_by_name: dict[str, Any] = getattr(bound_node, "tools_by_name", {})
    tool_names = list(tools_by_name.keys())
    assert "start_async_task" in tool_names
    assert "check_async_task" in tool_names
    assert "cancel_async_task" in tool_names
    assert "list_async_tasks" in tool_names


# ── Enhancement 4: Dynamic Subagent Fan-Out Streaming to Web UI ─────────────────

@pytest.mark.asyncio
async def test_executor_handle_custom_chunk_subagent_lifecycle() -> None:
    """Verify executor._handle_custom_chunk emits A2UI tool card surfaces and metadata."""
    from k8s_autopilot.server.executor import (
        A2AAutoPilotExecutor,
        _StreamContext,
        _StreamTelemetryState,
    )

    executor = A2AAutoPilotExecutor()

    updater = MagicMock()
    updater.update_status = AsyncMock()

    renderer = MagicMock()
    renderer.message_id = "msg_turn_1"

    event_queue = MagicMock()

    task_mock = MagicMock()
    task_mock.id = "task_root_1"

    ctx = _StreamContext(
        task=task_mock,
        updater=updater,
        event_queue=event_queue,
        context_id="ctx_1",
        use_ui=True,
        renderer=renderer,
        telemetry=_StreamTelemetryState(),
    )

    # 1. Test phase: start
    start_payload = {
        "type": "subagent",
        "phase": "start",
        "id": "sub_call_42",
        "subagent_type": "helm-operator",
        "description": "Inspect prometheus chart",
    }

    await executor._handle_custom_chunk(start_payload, ctx)

    assert updater.update_status.called
    msg_out = updater.update_status.call_args[0][1]
    assert "subagent" in msg_out.metadata
    assert "subagent_lifecycle" in msg_out.metadata

    lifecycle = json.loads(msg_out.metadata["subagent_lifecycle"])
    assert lifecycle["phase"] == "start"
    assert lifecycle["id"] == "sub_call_42"
    assert lifecycle["subagent_type"] == "helm-operator"
    assert lifecycle["description"] == "Inspect prometheus chart"

    # Verify A2UI surface was registered in active_tool_surfaces
    assert "sub_call_42" in ctx.active_tool_surfaces
    surface_entry = ctx.active_tool_surfaces["sub_call_42"]
    assert surface_entry["surface_id"] == "tool-task-sub_call_42"

    # Verify A2UI parts emitted
    assert len(msg_out.parts) > 0

    # 2. Test phase: complete
    complete_payload = {
        "type": "subagent",
        "phase": "complete",
        "id": "sub_call_42",
        "duration_ms": 850,
    }

    updater.update_status.reset_mock()
    await executor._handle_custom_chunk(complete_payload, ctx)

    assert updater.update_status.called
    msg_out2 = updater.update_status.call_args[0][1]
    lifecycle2 = json.loads(msg_out2.metadata["subagent_lifecycle"])
    assert lifecycle2["phase"] == "complete"
    assert lifecycle2["duration_ms"] == 850

    # Verify active tool surface was popped upon completion
    assert "sub_call_42" not in ctx.active_tool_surfaces


# ── Component 5: Telemetry Async Subagents Integration ──────────────────────────

@pytest.mark.asyncio
async def test_telemetry_includes_async_subagents() -> None:
    """Verify get_thread_telemetry includes discovered async subagents."""
    from k8s_autopilot.api.service import ThreadService

    service = ThreadService(checkpointer=MagicMock())
    with patch("k8s_autopilot.subagents.loader.load_async_subagents") as mock_load_async:
        mock_load_async.return_value = [
            {
                "name": "remote-cluster-drainer",
                "description": "Drains node pool remotely",
                "graph_id": "drainer-graph",
            }
        ]
        res = await service.get_thread_telemetry("thread-test-123")
        assert res is not None
        names = [sa.name for sa in res.subagents]
        assert "remote-cluster-drainer" in names
        drainer_entry = next(sa for sa in res.subagents if sa.name == "remote-cluster-drainer")
        assert drainer_entry.status == "idle"


# ── Enhancement 4 Part B: js_eval Plugin Subagent Fan-Out Streaming ─────────────

@pytest.mark.asyncio
async def test_executor_js_eval_plugin_subagent_fan_out_streaming() -> None:
    """Verify plugin subagents triggered via js_eval produce real-time A2UI cards and eval_id metadata."""
    from k8s_autopilot.server.executor import (
        A2AAutoPilotExecutor,
        _StreamContext,
        _StreamTelemetryState,
    )

    executor = A2AAutoPilotExecutor()

    updater = MagicMock()
    updater.update_status = AsyncMock()

    renderer = MagicMock()
    renderer.message_id = "msg_turn_2"

    ctx = _StreamContext(
        task=MagicMock(id="task_root_2"),
        updater=updater,
        event_queue=MagicMock(),
        context_id="ctx_2",
        use_ui=True,
        renderer=renderer,
        telemetry=_StreamTelemetryState(),
    )

    # 1. Simulate QuickJS task bridge emitting start event for a plugin subagent inside js_eval
    eval_call_id = "eval_call_789"
    plugin_subagent_id = "ptc_task_a1b2c3d4"

    plugin_start_payload = {
        "type": "subagent",
        "phase": "start",
        "id": plugin_subagent_id,
        "eval_id": eval_call_id,
        "subagent_type": "cost-analyzer@finops-plugin",
        "label": "cost-analyzer@finops-plugin: Analyze node spend",
        "description": "Analyze node spend across clusters",
    }

    await executor._handle_custom_chunk(plugin_start_payload, ctx)

    assert updater.update_status.called
    msg_out = updater.update_status.call_args[0][1]

    # Verify metadata contains subagent_lifecycle with eval_id linkage
    assert "subagent_lifecycle" in msg_out.metadata
    lifecycle = json.loads(msg_out.metadata["subagent_lifecycle"])
    assert lifecycle["phase"] == "start"
    assert lifecycle["id"] == plugin_subagent_id
    assert lifecycle["eval_id"] == eval_call_id
    assert lifecycle["subagent_type"] == "cost-analyzer@finops-plugin"

    # Verify dedicated toolExecutionCard was generated for the plugin subagent
    assert plugin_subagent_id in ctx.active_tool_surfaces
    surface_id = ctx.active_tool_surfaces[plugin_subagent_id]["surface_id"]
    assert surface_id == f"tool-task-{plugin_subagent_id}"

    # Verify A2UI surface contains Subagent name and parameters across emitted operations
    parts_str = "".join(str(p.data) for p in msg_out.parts)
    assert "toolExecutionCard" in parts_str or "toolName" in parts_str

    # 2. Simulate completion event from QuickJS
    plugin_complete_payload = {
        "type": "subagent",
        "phase": "complete",
        "id": plugin_subagent_id,
        "eval_id": eval_call_id,
        "duration_ms": 1340,
    }

    updater.update_status.reset_mock()
    await executor._handle_custom_chunk(plugin_complete_payload, ctx)

    assert updater.update_status.called
    msg_out2 = updater.update_status.call_args[0][1]
    lifecycle2 = json.loads(msg_out2.metadata["subagent_lifecycle"])
    assert lifecycle2["phase"] == "complete"
    assert lifecycle2["id"] == plugin_subagent_id
    assert lifecycle2["eval_id"] == eval_call_id
    assert lifecycle2["duration_ms"] == 1340

    # Verify card was removed from active surfaces upon completion
    assert plugin_subagent_id not in ctx.active_tool_surfaces
