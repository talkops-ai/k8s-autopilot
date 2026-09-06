"""Unit tests for ToolFilterMiddleware."""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.messages import ToolMessage

from k8s_autopilot.middleware.tool_filter import ToolFilterMiddleware, _expand_tool_patterns


class TestToolFilterMiddleware:
    def test_expand_aliases(self) -> None:
        expanded = _expand_tool_patterns(["read", "bash", "ls", "glob"])
        assert "read_file" in expanded
        assert "view_file" in expanded
        assert "run_command" in expanded
        assert "execute" in expanded
        assert "ls" in expanded
        assert "list_dir" in expanded
        assert "dir_list" in expanded

    def test_is_tool_allowed_unrestricted(self) -> None:
        mw = ToolFilterMiddleware(allowed_patterns=None)
        assert mw.is_tool_allowed("any_tool")
        assert mw.is_tool_allowed("execute")

    def test_is_tool_allowed_with_patterns(self) -> None:
        mw = ToolFilterMiddleware(allowed_patterns=["read_*", "grep*"])
        assert mw.is_tool_allowed("read_file")
        assert mw.is_tool_allowed("grep_search")
        assert not mw.is_tool_allowed("write_file")
        assert not mw.is_tool_allowed("execute")

    def test_wrap_tool_call_allowed(self) -> None:
        mw = ToolFilterMiddleware(allowed_patterns=["read_file"])
        request = MagicMock()
        request.tool_call = {"name": "read_file", "id": "call-1", "args": {"file_path": "a.txt"}}
        handler = MagicMock(return_value=ToolMessage(content="content", tool_call_id="call-1"))

        result = mw.wrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "content"
        handler.assert_called_once_with(request)

    def test_wrap_tool_call_rejected(self) -> None:
        mw = ToolFilterMiddleware(allowed_patterns=["read_file"])
        request = MagicMock()
        request.tool_call = {"name": "write_file", "id": "call-2", "args": {"file_path": "a.txt"}}
        handler = MagicMock()

        result = mw.wrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "Tool call rejected" in str(result.content)
        handler.assert_not_called()
