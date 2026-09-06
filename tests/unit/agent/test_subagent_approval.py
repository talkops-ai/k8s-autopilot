"""Unit tests for subagent approval context propagation and generic MCP tool gating."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from k8s_autopilot.agent.factory import _should_interrupt_tool_call
from k8s_autopilot.middleware.auto_mode_hitl import DynamicInterruptMapping, AsyncApprovalHITLMiddleware
from k8s_autopilot.security.approval_mode import ApprovalMode
from k8s_autopilot.security.approval_mode_source import _resolve_approval_mode, ApprovalPolicyResolver


def test_should_interrupt_tool_call_auto_mode():
    """Verify tools do not pause for human approval in AUTO mode and require confirmation in MANUAL mode."""
    # 1. Read-only tool under AUTO mode -> False
    req_readonly = {
        "state": {"approval_mode": "auto"},
        "action": {"name": "talkops-helm-mcp-server:kubernetes_get_helm_releases", "args": {}},
    }
    assert _should_interrupt_tool_call(req_readonly) is False

    # 2. Mutating tool under AUTO mode (Tier 3) -> False
    req_mutating = {
        "state": {"approval_mode": "auto"},
        "action": {"name": "talkops-helm-mcp-server:kubernetes_install_helm_chart", "args": {"chart": "nginx"}},
    }
    assert _should_interrupt_tool_call(req_mutating) is False

    # 3. In AUTO mode -> False (full autonomous execution)
    req_destructive_auto = {
        "state": {"approval_mode": "auto"},
        "action": {"name": "talkops-helm-mcp-server:kubernetes_uninstall_helm_release", "args": {"release": "nginx"}},
    }
    assert _should_interrupt_tool_call(req_destructive_auto) is False

    # 4. In MANUAL mode: read-only tool -> False
    req_readonly_manual = {
        "state": {"approval_mode": "manual"},
        "action": {"name": "talkops-helm-mcp-server:kubernetes_get_helm_releases", "args": {}},
    }
    assert _should_interrupt_tool_call(req_readonly_manual) is False

    # 5. In MANUAL mode: mutating/destructive tool -> True
    req_destructive_manual = {
        "state": {"approval_mode": "manual"},
        "action": {"name": "talkops-helm-mcp-server:kubernetes_uninstall_helm_release", "args": {"release": "nginx"}},
    }
    assert _should_interrupt_tool_call(req_destructive_manual) is True

    # 6. Destructive tool under YOLO mode -> False
    req_yolo = {
        "state": {"approval_mode": "yolo"},
        "action": {"name": "talkops-helm-mcp-server:kubernetes_uninstall_helm_release", "args": {"release": "nginx"}},
    }
    assert _should_interrupt_tool_call(req_yolo) is False


def test_approval_resolution_from_runtime_config():
    """Verify child subagent resolves approval mode from runtime.config when context is empty."""
    # Context is None/empty, but configurable dict has approval_mode
    runtime_mock = MagicMock()
    runtime_mock.context = None
    runtime_mock.config = {
        "configurable": {
            "thread_id": "thread-child-123",
            "approval_mode": "auto",
        }
    }
    runtime_mock.store = None

    req = {
        "runtime": runtime_mock,
        "action": {"name": "kubernetes_get_helm_releases", "args": {}},
    }
    assert _should_interrupt_tool_call(req) is False


def test_resolve_source_nested_configurable():
    """Verify ApprovalPolicyResolver extracts mode from nested configurable dict."""
    config = {
        "configurable": {
            "thread_id": "test-th-1",
            "approval_mode": "auto",
        }
    }
    source = ApprovalPolicyResolver.resolve_source(config)
    assert getattr(source, "mode", None) == ApprovalMode.AUTO


def test_dynamic_interrupt_mapping_generic_readonly():
    """Verify DynamicInterruptMapping identifies read-only tools across any MCP server."""
    dim = DynamicInterruptMapping()

    # Tools from various domains
    assert dim.is_readonly_tool("kubernetes_get_pods") is True
    assert dim.is_readonly_tool("helm_list") is True
    assert dim.is_readonly_tool("argocd_get_app") is True
    assert dim.is_readonly_tool("prometheus_query_range") is True
    assert dim.is_readonly_tool("aws_ec2_describe_instances") is True
    assert dim.is_readonly_tool("talkops-helm-mcp-server:kubernetes_get_helm_releases") is True

    # Destructive tools
    assert dim.is_readonly_tool("kubernetes_delete_pod") is False
    assert dim.is_readonly_tool("helm_uninstall") is False
    assert dim.is_readonly_tool("argocd_delete_app") is False
    assert dim.is_readonly_tool("cluster_drain_node") is False


def test_subagent_inherits_approval_mode_from_state_with_no_store():
    """Verify subagent executes mutating tool in auto mode when mode is present in state even if store is None."""
    runtime_mock = MagicMock()
    runtime_mock.context = None
    runtime_mock.config = {"configurable": {"thread_id": "child-subagent-thread"}}
    runtime_mock.store = None
    runtime_mock.state = {"approval_mode": "auto"}

    req = {
        "runtime": runtime_mock,
        "state": runtime_mock.state,
        "action": {"name": "talkops-helm-mcp-server:kubernetes_install_helm_chart", "args": {"chart": "nginx"}},
    }
    # Mutating tool under inherited AUTO mode from state must NOT interrupt!
    assert _should_interrupt_tool_call(req) is False


def test_subagent_namespaced_thread_resolves_root_from_service_store(monkeypatch):
    """Verify subagent with namespaced thread id (e.g. parent:subagent) resolves approval mode from thread service."""
    from k8s_autopilot.security.approval_mode import approval_mode_key

    parent_thread_id = "parent-sess-456"
    child_thread_id = f"{parent_thread_id}:task:helm-operator"
    root_key = approval_mode_key(parent_thread_id)

    from unittest.mock import AsyncMock

    mock_service = MagicMock()
    mock_checkpointer = MagicMock(spec=["store"])
    mock_store = MagicMock()
    lookup = lambda ns, k: {"value": {"mode": "auto"}} if k == root_key else None
    mock_store.get.side_effect = lookup
    mock_store.aget = AsyncMock(side_effect=lookup)
    mock_checkpointer.store = mock_store
    mock_service._checkpointer = mock_checkpointer

    monkeypatch.setattr("k8s_autopilot.api.service.get_thread_service", lambda: mock_service)

    runtime_mock = MagicMock()
    runtime_mock.context = None
    runtime_mock.config = {"configurable": {"thread_id": child_thread_id}}
    runtime_mock.store = None  # Subagent has no local store

    req = {
        "runtime": runtime_mock,
        "action": {"name": "talkops-helm-mcp-server:kubernetes_install_helm_chart", "args": {"chart": "nginx"}},
    }
    # Should resolve root store key from ThreadService and allow execution without interrupt!
    assert _should_interrupt_tool_call(req) is False


def test_read_approval_mode_from_store_guards_against_checkpointer():
    """Verify read_approval_mode_from_store safely handles BaseCheckpointSaver without raising TypeError."""
    from k8s_autopilot.security.approval_mode import read_approval_mode_from_store
    from langgraph.checkpoint.base import BaseCheckpointSaver

    class FakeCheckpointer(BaseCheckpointSaver):
        def get(self, config):
            raise TypeError("BaseCheckpointSaver.get() takes 2 positional arguments but 3 were given")

        def get_tuple(self, config):
            return None

        def list(self, config, *, filter=None, before=None, limit=None):
            return iter([])

        def put(self, config, checkpoint, metadata, new_versions):
            return {}

        def put_writes(self, config, writes, task_id):
            pass

    saver = FakeCheckpointer()
    assert read_approval_mode_from_store(saver, "some_key") is None


