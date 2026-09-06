"""Unit tests for ServerHooksMiddleware and Hooks v2 transport models."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from k8s_autopilot.hooks.models import (
    HookEvent,
    PermissionEffect,
    PreToolUseDecision,
    ToolCallData,
)
from k8s_autopilot.hooks.tools import to_wire_call, to_wire_tool_name
from k8s_autopilot.middleware.server_hooks import (
    ServerHooksMiddleware,
    ServerHooksState,
    _event_enabled,
    _session_gate,
    hook_decided_permission,
)


class TestHooksWireTools:
    def test_native_to_wire_tool_name(self) -> None:
        assert to_wire_tool_name("execute") == "Bash"
        assert to_wire_tool_name("write_file") == "Write"
        assert to_wire_tool_name("edit_file") == "Edit"
        assert to_wire_tool_name("read_file") == "Read"
        assert to_wire_tool_name("glob") == "Glob"
        assert to_wire_tool_name("grep") == "Grep"
        assert to_wire_tool_name("custom_tool") == "custom_tool"

    def test_mcp_wire_name_mapping(self) -> None:
        assert to_wire_tool_name("fetch", mcp_server="k8s") == "mcp__k8s__fetch"
        assert to_wire_tool_name("mcp__k8s__fetch") == "mcp__k8s__fetch"

    def test_to_wire_call(self) -> None:
        call = ToolCallData(
            id="call-1",
            name="execute",
            args={"command": "kubectl get pods", "timeout": 10},
        )
        wire_name, wire_args = to_wire_call(call)
        assert wire_name == "Bash"
        assert wire_args["command"] == "kubectl get pods"
        assert wire_args["timeout"] == 10000


class TestServerHooksMiddleware:
    def test_session_gate(self) -> None:
        context = {
            "hooks_snapshot_id": "snap-123",
            "hooks_server_events": ["PreToolUse", "PostToolUse"],
        }
        gate = _session_gate(context)
        assert gate is not None
        assert gate["snapshot_id"] == "snap-123"
        assert _event_enabled(gate, HookEvent.PRE_TOOL_USE)
        assert not _event_enabled(gate, HookEvent.STOP)

    def test_session_gate_empty(self) -> None:
        assert _session_gate({}) is None
        assert _session_gate(None) is None

    def test_before_model_disabled_events_returns_empty(self, tmp_path: Path) -> None:
        mw = ServerHooksMiddleware(cwd=tmp_path)
        runtime = MagicMock()
        runtime.context = {}
        state: ServerHooksState = {"messages": []}
        update = mw.before_model(state, runtime)
        assert update == {"_hooks_pre_tool_outcomes": {}}

    def test_hook_decided_permission(self) -> None:
        state = {
            "_hooks_pre_tool_outcomes": {
                "call-1": {"behavior": "allow", "context": []},
                "call-2": {"behavior": "deny", "reason": "Blocked", "context": []},
            }
        }
        assert hook_decided_permission(state, "call-1") is True
        assert hook_decided_permission(state, "call-2") is True
        assert hook_decided_permission(state, "call-3") is False
