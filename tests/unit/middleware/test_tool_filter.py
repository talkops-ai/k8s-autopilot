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

    def test_is_tool_allowed_with_server_prefix(self) -> None:
        mw = ToolFilterMiddleware(allowed_patterns=["*refresh*", "*sync*", "*_application*"])
        assert mw.is_tool_allowed("talkops-argocd-mcp-server:soft_refresh")
        assert mw.is_tool_allowed("talkops-argocd-mcp-server:hard_refresh")
        assert mw.is_tool_allowed("talkops-argocd-mcp-server:sync_application")
        assert not mw.is_tool_allowed("talkops-argocd-mcp-server:delete_cluster")

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

    def test_is_tool_allowed_with_declarative_capabilities(self) -> None:
        capabilities = [
            {"mcp_server": "talkops-argocd-mcp-server", "allow_all": True},
            {"mcp_server": "talkops-argo-rollout-mcp-server", "allow_all": True},
            {"tools": ["read", "execute"]},
        ]
        mw = ToolFilterMiddleware(capabilities=capabilities)

        # Server allow_all permits any tool on that server without fnmatch guessing
        assert mw.is_tool_allowed("talkops-argocd-mcp-server:soft_refresh")
        assert mw.is_tool_allowed("talkops-argocd-mcp-server:hard_refresh")
        assert mw.is_tool_allowed("talkops-argocd-mcp-server:prune_resources")
        assert mw.is_tool_allowed("talkops-argo-rollout-mcp-server:promote_rollout")

        # Specific tools and aliases allowed
        assert mw.is_tool_allowed("read_file")
        assert mw.is_tool_allowed("view_file")
        assert mw.is_tool_allowed("run_command")

        # Unpermitted server blocked
        assert not mw.is_tool_allowed("unpermitted-mcp-server:delete_cluster")

    def test_app_operator_subagent_carries_tools(self) -> None:
        from pathlib import Path
        from k8s_autopilot.subagents.loader import _parse_subagent_file

        app_op_path = Path("k8s_autopilot/built_in_subagents/app-operator/agents/app-operator.md")
        if app_op_path.exists():
            meta = _parse_subagent_file(app_op_path)
            assert meta is not None
            tools_list = meta.get("tools")
            assert tools_list is not None
            assert any("mcp__talkops-argocd-mcp-server__*" in t for t in tools_list)
            assert any("mcp__talkops-argo-rollout-mcp-server__*" in t for t in tools_list)
            assert any("mcp__talkops-traefik-mcp-server__*" in t for t in tools_list)

            mw = ToolFilterMiddleware(
                allowed_patterns=meta.get("tools"),
            )
            # Standard developer tools
            assert mw.is_tool_allowed("grep")
            assert mw.is_tool_allowed("grep_search")
            assert mw.is_tool_allowed("edit_file")
            assert mw.is_tool_allowed("replace_file_content")
            assert mw.is_tool_allowed("read_file")
            assert mw.is_tool_allowed("write_file")
            assert mw.is_tool_allowed("ls")
            assert mw.is_tool_allowed("glob")
            assert mw.is_tool_allowed("execute")
            assert mw.is_tool_allowed("run_command")

            # MCP tools under allowed servers via wire format (mcp__ prefix)
            assert mw.is_tool_allowed("mcp__talkops-argocd-mcp-server__get_sync_status")
            assert mw.is_tool_allowed("mcp__talkops-argocd-mcp-server__soft_refresh")
            assert mw.is_tool_allowed("mcp__talkops-argo-rollout-mcp-server__promote_rollout")
            assert mw.is_tool_allowed("mcp__talkops-traefik-mcp-server__list_routers")

            # MCP tools under allowed servers via colon format
            assert mw.is_tool_allowed("talkops-argocd-mcp-server:get_sync_status")
            assert mw.is_tool_allowed("talkops-argo-rollout-mcp-server:promote_rollout")

            # Unpermitted server blocked
            assert not mw.is_tool_allowed("mcp__unpermitted-mcp-server__delete_cluster")
            assert not mw.is_tool_allowed("unpermitted-mcp-server:delete_cluster")

    def test_grep_and_edit_alias_expansion(self) -> None:
        mw = ToolFilterMiddleware(allowed_patterns=["grep", "Edit"])
        assert mw.is_tool_allowed("grep")
        assert mw.is_tool_allowed("grep_search")
        assert mw.is_tool_allowed("edit_file")
        assert mw.is_tool_allowed("replace_file_content")
        assert mw.is_tool_allowed("multi_replace_file_content")
        assert not mw.is_tool_allowed("write_file")
