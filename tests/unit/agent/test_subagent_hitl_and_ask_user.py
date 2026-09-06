from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from k8s_autopilot.agent.factory import (
    _format_description,
    _should_interrupt_tool_call,
    _subagent_cli_middleware,
    create_k8s_autopilot_agent,
)
from k8s_autopilot.mcp.session_manager import _build_cached_mcp_tool
from k8s_autopilot.middleware.ask_user import AskUserMiddleware
from k8s_autopilot.middleware.auto_mode import (
    AsyncApprovalHITLMiddleware,
    DynamicInterruptMapping,
    READONLY_SAFE_TOOLS,
)
from k8s_autopilot.security.approval_mode import ApprovalMode


class TestDynamicInterruptMapping:
    """Tests for zero-hardcoding dynamic tool gating."""

    def test_dynamic_interception_for_arbitrary_tools(self) -> None:
        dim = DynamicInterruptMapping()
        # Any dynamic tool from plugin, skill, or MCP should be intercepted
        assert "talkops-helm-mcp-server:helm_install_chart" in dim
        assert "talkops-kubernetes-mcp-server:delete_pod" in dim
        assert "partner_fin_plugin_transfer" in dim
        assert "custom_dynamic_agent_tool" in dim
        assert "execute" in dim
        assert "write_file" in dim

    def test_readonly_tools_excluded_from_interruption(self) -> None:
        dim = DynamicInterruptMapping()
        # Inspection and safe tools should NOT trigger HITL gating
        for safe in READONLY_SAFE_TOOLS:
            assert safe not in dim
            assert dim.get(safe) is None

        assert "get_cluster_info_get" not in dim
        assert "inspect_nodes_list" not in dim
        assert "check_health_status" not in dim

    def test_get_and_getitem_return_default_config(self) -> None:
        default_cfg = {
            "allowed_decisions": ["approve", "reject"],
            "description": "test",
        }
        dim = DynamicInterruptMapping(default_config=default_cfg)

        cfg = dim.get("arbitrary_new_skill_action")
        assert cfg == default_cfg

        cfg_item = dim["talkops-argocd-mcp-server:sync_app"]
        assert cfg_item == default_cfg

        # Read-only tools return None / raise KeyError
        assert dim.get("ask_user") is None
        with pytest.raises(KeyError):
            _ = dim["read_file"]


class TestSubagentMiddlewareStack:
    """Tests verifying subagent middleware composition."""

    def test_subagent_receives_ask_user_middleware(self) -> None:
        mw_list = _subagent_cli_middleware(
            has_explicit_model=False,
            assistant_id="k8s-autopilot",
            subagent_name="helm-operator",
            interactive=True,
        )
        ask_user_mw = [m for m in mw_list if isinstance(m, AskUserMiddleware)]
        assert len(ask_user_mw) == 1

    def test_subagent_receives_async_approval_hitl_at_index_0(self) -> None:
        dim = DynamicInterruptMapping()
        mw_list = _subagent_cli_middleware(
            has_explicit_model=False,
            assistant_id="k8s-autopilot",
            subagent_name="helm-operator",
            interactive=True,
            interrupt_on=dim,
        )
        assert isinstance(mw_list[0], AsyncApprovalHITLMiddleware)
        assert isinstance(mw_list[0].interrupt_on, DynamicInterruptMapping)


class TestIntelligentToolEvaluation:
    """Tests for _should_interrupt_tool_call with dynamic approval modes."""

    def test_yolo_mode_never_interrupts(self) -> None:
        req = {
            "state": {"approval_mode": "yolo"},
            "action": {"name": "talkops-helm-mcp-server:helm_uninstall", "args": {"release": "prod"}},
        }
        assert _should_interrupt_tool_call(req) is False

    def test_manual_mode_interrupts_mutating_tools(self) -> None:
        req = {
            "state": {"approval_mode": "manual"},
            "action": {"name": "talkops-helm-mcp-server:helm_install_chart", "args": {"name": "nginx"}},
        }
        assert _should_interrupt_tool_call(req) is True

    def test_manual_mode_skips_internal_agents_md(self) -> None:
        req = {
            "state": {"approval_mode": "manual"},
            "action": {"name": "write_file", "args": {"file_path": "/workspace/AGENTS.md"}},
        }
        assert _should_interrupt_tool_call(req) is False

    def test_default_without_explicit_mode_interrupts(self) -> None:
        # If no mode is provided anywhere, fail closed to manual -> interrupt
        from k8s_autopilot.config.settings import get_settings
        get_settings().approval_mode = "manual"
        req = {
            "action": {"name": "talkops-helm-mcp-server:helm_upgrade_release", "args": {"release": "argocd"}},
        }
        assert _should_interrupt_tool_call(req) is True

    def test_auto_mode_evaluation_with_enabled_flag(self) -> None:
        req = {
            "state": {"approval_mode": "auto"},
            "action": {"name": "execute", "args": {"command": "kubectl apply -f app.yaml"}},
        }
        # In AUTO mode, non-destructive tools execute automatically without interruption
        assert _should_interrupt_tool_call(req) is False

    def test_subagent_empty_runtime_fails_closed_to_manual(self) -> None:
        mock_runtime = MagicMock()
        mock_runtime.context = None
        mock_runtime.store = None
        req = {
            "action": {"name": "talkops-helm-mcp-server:helm_upgrade_release", "args": {"release": "argocd"}},
            "runtime": mock_runtime,
        }
        assert _should_interrupt_tool_call(req) is True

    def test_format_description_for_mcp_and_custom_tools(self) -> None:
        mcp_desc = _format_description(
            {"name": "talkops-helm-mcp-server:helm_install", "args": {"chart": "redis", "namespace": "cache"}}
        )
        assert "talkops-helm-mcp-server" in mcp_desc
        assert "helm_install" in mcp_desc
        assert "chart=redis" in mcp_desc

        custom_desc = _format_description(
            {"name": "custom_financial_settlement", "args": {"amount": "1000", "currency": "USD"}}
        )
        assert "custom_financial_settlement" in custom_desc
        assert "amount=1000" in custom_desc


class TestSubagentHITLInterruptResolution:
    """Async approval resolution tests simulating subagent execution."""

    @pytest.mark.asyncio
    async def test_subagent_aafter_model_with_none_context_resolves_manual(self) -> None:
        from unittest.mock import patch

        dim = DynamicInterruptMapping()
        mw = AsyncApprovalHITLMiddleware(dim)

        mock_runtime = MagicMock()
        mock_runtime.context = None
        mock_runtime.store = None

        tool_call = {
            "id": "tc_123",
            "name": "talkops-helm-mcp-server:helm_upgrade_release",
            "args": {"release": "argocd", "chart": "argo/argo-cd"},
        }
        ai_msg = AIMessage(content="", tool_calls=[tool_call])
        initial_state = {"messages": [ai_msg]}

        with patch("langchain.agents.middleware.human_in_the_loop.interrupt") as mock_interrupt:
            mock_interrupt.return_value = {"decisions": [{"type": "approve"}]}
            result = await mw.aafter_model(cast(Any, initial_state), mock_runtime)

            assert mock_interrupt.called
            hitl_req = mock_interrupt.call_args[0][0]
            action_reqs = hitl_req.action_requests if hasattr(hitl_req, "action_requests") else hitl_req["action_requests"]
            first_action = action_reqs[0]
            name = first_action.name if hasattr(first_action, "name") else first_action["name"]
            args = first_action.args if hasattr(first_action, "args") else first_action["args"]
            assert name == "talkops-helm-mcp-server:helm_upgrade_release"
            assert args == {"release": "argocd", "chart": "argo/argo-cd"}


class TestMCPToolAnnotationAndMarker:
    """Tests verifying MCP tool metadata annotation propagation."""

    def test_build_cached_mcp_tool_attaches_marker_and_annotations(self) -> None:
        mock_mcp_tool = MagicMock()
        mock_mcp_tool.name = "helm_install"
        mock_mcp_tool.description = "Install a helm chart"
        mock_mcp_tool.inputSchema = {"type": "object", "properties": {"chart": {"type": "string"}}}
        mock_mcp_tool.annotations = {"readOnlyHint": False, "destructiveHint": True}

        mock_mgr = MagicMock()
        lc_tool = _build_cached_mcp_tool(
            mcp_tool=mock_mcp_tool,
            server_name="talkops-helm-mcp-server",
            session_manager=mock_mgr,
        )

        assert lc_tool.name == "talkops-helm-mcp-server:helm_install"
        assert lc_tool.metadata is not None
        assert lc_tool.metadata.get("_k8s_autopilot_mcp") is True
        assert lc_tool.metadata.get("_mcp_server") == "talkops-helm-mcp-server"
        assert lc_tool.metadata.get("readOnlyHint") is False
        assert lc_tool.metadata.get("destructiveHint") is True


class TestSubagentFormattingAndEvents:
    """Tests for subagent tool formatting, HITL decision logic, and lifecycle event streaming."""

    def test_format_description_for_subagent_task(self) -> None:
        call = {
            "name": "task",
            "args": {
                "subagent_type": "helm-operator",
                "description": "List all Helm releases in cluster",
            },
        }
        desc = _format_description(call)
        assert desc == "Spawn helm-operator subagent: List all Helm releases in cluster"

    def test_format_description_for_js_eval(self) -> None:
        call = {
            "name": "js_eval",
            "args": {"code": "spawnSubagent('observability-operator', 'check metrics');"},
        }
        desc = _format_description(call)
        assert "Evaluate script / dispatch subagents: spawnSubagent('observability-operator'" in desc

    def test_should_interrupt_subagent_in_manual_and_yolo(self) -> None:
        req_manual = {
            "name": "task",
            "args": {"subagent_type": "helm-operator", "description": "list releases"},
            "approval_mode": "manual",
        }
        # In manual mode, spawning a subagent requires approval
        assert _should_interrupt_tool_call(req_manual) is True

        req_yolo = {
            "name": "task",
            "args": {"subagent_type": "helm-operator", "description": "list releases"},
            "approval_mode": "yolo",
        }
        # In yolo mode, subagents execute without interruption
        assert _should_interrupt_tool_call(req_yolo) is False

    @pytest.mark.asyncio
    async def test_server_hooks_dispatches_subagent_events(self) -> None:
        from pathlib import Path
        from k8s_autopilot.middleware.server_hooks import ServerHooksMiddleware
        from langchain_core.messages import ToolMessage
        from unittest.mock import patch, MagicMock

        mw = ServerHooksMiddleware(cwd=Path.cwd())
        mock_req = MagicMock()
        mock_req.runtime.context = {}
        mock_req.runtime.config = {"configurable": {"thread_id": "test-thread"}}
        mock_req.state = {"messages": []}
        mock_req.tool_call = {
            "id": "call-123",
            "name": "task",
            "args": {
                "subagent_type": "helm-operator",
                "description": "List all helm releases",
            },
        }

        async def mock_handler(_req):
            return ToolMessage(
                content="Found 2 releases: my-app, postgres",
                name="task",
                tool_call_id="call-123",
                status="success",
            )

        events_dispatched = []

        with patch("langchain_core.callbacks.manager.adispatch_custom_event") as mock_dispatch:
            async def fake_dispatch(event_name, data, config=None):
                events_dispatched.append((event_name, data))

            mock_dispatch.side_effect = fake_dispatch
            res = await mw.awrap_tool_call(mock_req, mock_handler)

            assert isinstance(res, ToolMessage)
            assert "Found 2 releases" in res.content
            assert len(events_dispatched) >= 2
            assert events_dispatched[0][0] == "subagent"
            assert events_dispatched[0][1]["phase"] == "start"
            assert events_dispatched[0][1]["subagent_type"] == "helm-operator"
            assert events_dispatched[1][0] == "subagent"
            assert events_dispatched[1][1]["phase"] == "complete"
            assert events_dispatched[1][1]["id"] == "call-123"

    def test_subagent_inherits_auto_mode_from_thread_store(self) -> None:
        from k8s_autopilot.security.approval_mode import (
            APPROVAL_MODE_NAMESPACE,
            approval_mode_key,
        )
        from k8s_autopilot.security.approval_mode_source import _resolve_approval_mode

        thread_id = "thread-subagent-test-123"
        key = approval_mode_key(thread_id)

        mock_store = MagicMock()
        mock_item = MagicMock()
        mock_item.value = {"mode": "auto"}
        mock_store.get.return_value = mock_item

        # Subagent context with only thread_id/context_id
        child_context = {"context_id": thread_id}
        resolved = _resolve_approval_mode(child_context, mock_store)
        assert resolved == ApprovalMode.AUTO

        # Verify should_interrupt evaluates to False for child tools
        req = {
            "name": "kubernetes_get_helm_releases",
            "args": {"namespace": "default"},
            "runtime": MagicMock(context=child_context, store=mock_store),
        }
        assert _should_interrupt_tool_call(req) is False

