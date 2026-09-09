"""Unit tests for subagent system prompt unification and MCP server probe diagnostics."""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from deepagents.middleware._utils import append_to_system_message
from k8s_autopilot.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from k8s_autopilot.mcp.preload import _clean_stderr_diagnostic, _extract_root_mcp_error
from k8s_autopilot.middleware.ask_user import AskUserMiddleware
from k8s_autopilot.middleware.mcp_context import MCPContextMiddleware
from k8s_autopilot.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
    unify_system_message,
)


class TestUnifySystemMessage:
    """Tests for unify_system_message normalization and parsing fallbacks."""

    def test_unify_none_returns_none(self):
        assert unify_system_message(None) is None

    def test_unify_plain_string_preserved(self):
        msg = SystemMessage(content="Hello, world!")
        unified = unify_system_message(msg)
        assert unified is not None
        assert unified.content == "Hello, world!"

    def test_unify_content_blocks_list_joined(self):
        blocks: list[dict[Any, Any] | str] = [
            {"type": "text", "text": "<identity>You are App Operator</identity>"},
            {"type": "text", "text": "\n\n## Shell paths vs. virtual paths\n\nSome paths..."},
        ]
        msg = SystemMessage(content=blocks)
        unified = unify_system_message(msg)
        assert unified is not None
        assert isinstance(unified.content, str)
        assert "<identity>You are App Operator</identity>" in unified.content
        assert "## Shell paths vs. virtual paths" in unified.content
        assert "[{'type'" not in unified.content

    def test_unify_stringified_python_list_recovered(self):
        stringified = (
            "[{'type': 'text', 'text': '<identity>You are App Operator</identity>'}, "
            "{'type': 'text', 'text': '\\n\\n## Shell paths vs. virtual paths\\n\\nSome paths...'}]\n\n"
            "**MCP Servers** (1 servers, 1 tools):\n- **argocd** (stdio): sync"
        )
        msg = SystemMessage(content=stringified)
        unified = unify_system_message(msg)
        assert unified is not None
        assert isinstance(unified.content, str)
        assert "<identity>You are App Operator</identity>" in unified.content
        assert "## Shell paths vs. virtual paths" in unified.content
        assert "**MCP Servers** (1 servers, 1 tools):" in unified.content
        assert "[{'type'" not in unified.content


class TestMCPContextMiddleware:
    """Tests for MCPContextMiddleware request modification and formatting."""

    def test_mcp_context_appends_without_stringifying_blocks(self):
        # Initial message with content blocks (as emitted by deepagents FilesystemMiddleware)
        sys_msg = SystemMessage(content="<identity>You are App Operator</identity>")
        sys_msg = append_to_system_message(sys_msg, "## Shell paths vs. virtual paths")

        server = MCPServerInfo(
            name="argocd",
            transport="stdio",
            status="ok",
            tools=(MCPToolInfo(name="argocd:list_apps", description="", input_schema={}),),
            enabled=True,
        )

        fake_model = FakeMessagesListChatModel(responses=[])
        request = ModelRequest(
            model=fake_model,
            messages=[HumanMessage(content="run task")],
            system_message=sys_msg,
            tools=[],
        )

        mw = MCPContextMiddleware(mcp_server_info=[server])
        modified_req = mw._get_modified_request(request)

        assert modified_req.system_message is not None
        modified_content = modified_req.system_message.content
        if isinstance(modified_content, str):
            assert "[{'type'" not in modified_content
        else:
            assert isinstance(modified_content, list)
            for block in modified_content:
                if isinstance(block, dict):
                    assert "[{'type'" not in str(block.get("text", ""))

    def test_mcp_context_unavailable_server_prompt_guidance(self):
        server = MCPServerInfo(
            name="argo-rollouts",
            transport="stdio",
            status="error",
            error="Argo Rollouts CRD not found in cluster",
            tools=(),
            enabled=True,
        )
        mw = MCPContextMiddleware(mcp_server_info=[server])
        prompt_text = mw._build_mcp_context()

        assert "UNAVAILABLE" in prompt_text
        assert "<error>Argo Rollouts CRD not found in cluster</error>" in prompt_text
        assert "FAILED TO LOAD" not in prompt_text
        assert "suggest restarting the MCP server" not in prompt_text
        assert "only mention this if the user's task specifically requires" in prompt_text


class TestSubagentMiddlewareChainParity:
    """Tests end-to-end subagent middleware chain ensuring clean markdown system prompts."""

    def test_full_chain_produces_clean_markdown_string(self):
        sys_msg = SystemMessage(content="<identity>You are App Operator</identity>")
        sys_msg = append_to_system_message(sys_msg, "## Shell paths vs. virtual paths\n\nHost mappings...")

        server_ok = MCPServerInfo(
            name="talkops-argocd-mcp-server",
            transport="stdio",
            status="ok",
            tools=(MCPToolInfo(name="talkops-argocd-mcp-server:list_apps", description="", input_schema={}),),
            enabled=True,
        )
        server_err = MCPServerInfo(
            name="talkops-argo-rollout-mcp-server",
            transport="stdio",
            status="error",
            error="Argo Rollouts CRD not found",
            tools=(),
            enabled=True,
        )

        fake_model = FakeMessagesListChatModel(responses=[])
        req = ModelRequest(
            model=fake_model,
            messages=[HumanMessage(content="deploy app")],
            system_message=sys_msg,
            tools=[],
        )

        # 1. MCPContextMiddleware
        mcp_mw = MCPContextMiddleware(mcp_server_info=[server_ok, server_err])
        req = mcp_mw._get_modified_request(req)

        # 2. AskUserMiddleware
        ask_mw = AskUserMiddleware()
        req = ask_mw._with_ask_user_prompt(req)

        # 3. UnifiedSystemMessageMiddleware
        unify_mw = UnifiedSystemMessageMiddleware()
        req = unify_mw._normalize_request(req)

        assert req.system_message is not None
        final_content = req.system_message.content
        assert isinstance(final_content, str), "Subagent system prompt must be a pure string"
        assert "[{'type'" not in final_content
        assert "<identity>You are App Operator</identity>" in final_content
        assert "## Shell paths vs. virtual paths" in final_content
        assert "**MCP Servers** (2 servers, 1 tools):" in final_content
        assert "talkops-argocd-mcp-server:list_apps" in final_content
        assert "talkops-argo-rollout-mcp-server" in final_content
        assert "## `ask_user`" in final_content


class TestMCPStderrDiagnosticExtraction:
    """Tests for extracting meaningful process stderr diagnostics during MCP probing."""

    def test_clean_stderr_crd_not_found(self):
        stderr = (
            "Traceback (most recent call last):\n"
            "  File 'main.py', line 10, in <module>\n"
            "    cli()\n"
            "argo_rollout_mcp_server.exceptions.custom.KubernetesResourceError: Argo Rollouts CRD not found. Is Argo Rollouts installed?\n"
        )
        diag = _clean_stderr_diagnostic(stderr)
        assert diag is not None
        assert "Argo Rollouts CRD not found" in diag
        assert "Traceback" not in diag

    def test_clean_stderr_traefik_not_found(self):
        stderr = "Server error: Traefik IngressRoute CRD not found. Is Traefik installed?\n"
        diag = _clean_stderr_diagnostic(stderr)
        assert diag is not None
        assert "Traefik IngressRoute CRD not found" in diag

    def test_extract_root_mcp_error_uses_stderr_when_connection_closed(self):
        exc = Exception("Connection closed")
        stderr = "Server error: Traefik IngressRoute CRD not found. Is Traefik installed?\n"
        status, err_msg = _extract_root_mcp_error(exc, None, stderr_output=stderr)
        assert status == "error"
        assert "Traefik IngressRoute CRD not found" in err_msg
        assert err_msg != "Connection closed"

    def test_extract_root_mcp_error_fallback_without_stderr(self):
        exc = Exception("Connection closed")
        status, err_msg = _extract_root_mcp_error(exc, None, stderr_output=None)
        assert status == "error"
        assert err_msg == "Connection closed"
