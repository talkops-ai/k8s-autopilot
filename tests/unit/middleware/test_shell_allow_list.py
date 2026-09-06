"""Unit tests for ShellAllowListMiddleware."""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.messages import ToolMessage

from k8s_autopilot.middleware.shell_allow_list import ShellAllowListMiddleware


class TestShellAllowListMiddleware:
    def test_allowed_command_passes(self) -> None:
        mw = ShellAllowListMiddleware(allow_list=["kubectl get", "helm list"])
        request = MagicMock()
        request.tool_call = {"name": "execute", "id": "c-1", "args": {"command": "kubectl get pods -A"}}
        handler = MagicMock(return_value=ToolMessage(content="pods list", tool_call_id="c-1"))

        result = mw.wrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "pods list"
        handler.assert_called_once_with(request)

    def test_disallowed_command_rejected(self) -> None:
        mw = ShellAllowListMiddleware(allow_list=["kubectl get"])
        request = MagicMock()
        request.tool_call = {"name": "execute", "id": "c-2", "args": {"command": "kubectl delete pod nginx"}}
        handler = MagicMock()

        result = mw.wrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "not in the allow-list" in str(result.content)
        handler.assert_not_called()

    def test_empty_allow_list_rejects_all(self) -> None:
        mw = ShellAllowListMiddleware(allow_list=[])
        request = MagicMock()
        request.tool_call = {"name": "execute", "id": "c-3", "args": {"command": "ls"}}
        handler = MagicMock()

        result = mw.wrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "no commands are allowed" in str(result.content)
        handler.assert_not_called()

    def test_non_shell_tool_passes_through(self) -> None:
        mw = ShellAllowListMiddleware(allow_list=["kubectl get"])
        request = MagicMock()
        request.tool_call = {"name": "read_file", "id": "c-4", "args": {"file_path": "pod.yaml"}}
        handler = MagicMock(return_value=ToolMessage(content="yaml", tool_call_id="c-4"))

        result = mw.wrap_tool_call(request, handler)
        assert result.content == "yaml"
        handler.assert_called_once_with(request)
