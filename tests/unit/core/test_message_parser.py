"""Unit tests for MessageParser and universal message formats."""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from k8s_autopilot.server.message_parser import (
    MessageParser,
    MessageRole,
    ParsedMessage,
    ParsedToolCall,
    ToolCallStatus,
)
from k8s_autopilot.middleware.goal_state_notice import build_goal_state_notice


def test_parse_human_message() -> None:
    """Verify parsing HumanMessage."""
    msg = HumanMessage(content="Hello assistant", id="msg-1")
    parsed = MessageParser.parse_message(msg)
    assert parsed is not None
    assert parsed.role == MessageRole.USER
    assert parsed.content == "Hello assistant"
    assert parsed.id == "msg-1"
    assert parsed.is_control is False


def test_parse_ai_message_with_thinking_and_tool_calls() -> None:
    """Verify parsing AIMessage with thinking metadata and tool calls."""
    msg = AIMessage(
        content="Deploying application",
        id="msg-2",
        tool_calls=[
            {"id": "call-1", "name": "kubectl_apply", "args": {"file": "deployment.yaml"}}
        ],
        response_metadata={"thinking": "Need to apply manifest"},
    )
    parsed = MessageParser.parse_message(msg)
    assert parsed is not None
    assert parsed.role == MessageRole.ASSISTANT
    assert parsed.content == "Deploying application"
    assert parsed.thinking == "Need to apply manifest"
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].id == "call-1"
    assert parsed.tool_calls[0].name == "kubectl_apply"
    assert parsed.tool_calls[0].args == {"file": "deployment.yaml"}


def test_parse_tool_message() -> None:
    """Verify parsing ToolMessage with success/error status."""
    tool_ok = ToolMessage(content="Created successfully", tool_call_id="call-1", name="kubectl_apply")
    parsed_ok = MessageParser.parse_message(tool_ok)
    assert parsed_ok is not None
    assert parsed_ok.role == MessageRole.TOOL
    assert parsed_ok.content == "Created successfully"
    assert parsed_ok.tool_calls[0].status == ToolCallStatus.SUCCESS

    tool_err = ToolMessage(
        content="Error 404", tool_call_id="call-2", name="kubectl_get", status="error"
    )
    parsed_err = MessageParser.parse_message(tool_err)
    assert parsed_err is not None
    assert parsed_err.tool_calls[0].status == ToolCallStatus.ERROR


def test_parse_messages_sequence_and_tool_correlation() -> None:
    """Verify sequence parsing and correlation of tool call results with assistant tool calls."""
    human = HumanMessage(content="Apply manifest")
    ai = AIMessage(
        content="Applying...",
        tool_calls=[{"id": "c-1", "name": "apply", "args": {}}],
    )
    tool = ToolMessage(content="Applied OK", tool_call_id="c-1", name="apply")
    control = build_goal_state_notice({"goal_objective": "test"})

    # Without control messages
    parsed = MessageParser.parse_messages([human, ai, tool, control], include_control=False)
    assert len(parsed) == 3
    assert parsed[0].role == MessageRole.USER
    assert parsed[1].role == MessageRole.ASSISTANT
    # The assistant tool call status and result should be correlated from the ToolMessage
    assert parsed[1].tool_calls[0].status == ToolCallStatus.SUCCESS
    assert parsed[1].tool_calls[0].result == "Applied OK"

    # With control messages
    parsed_all = MessageParser.parse_messages([human, ai, tool, control], include_control=True)
    assert len(parsed_all) == 4
