"""Unit tests for GraphStreamBridge and model-agnostic thinking extraction."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)

from k8s_autopilot.integrations.stream_bridge import (
    GraphStreamBridge,
    StreamSink,
    _extract_text,
    _extract_text_and_thinking,
    _humanize_tool_name,
)
from k8s_autopilot.middleware.goal_state_notice import build_goal_state_notice


class DummySink:
    """Mock StreamSink capturing all lifecycle callbacks."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.thinking: list[str] = []
        self.tool_starts: list[tuple[str, str, dict[str, Any]]] = []
        self.tool_completes: list[tuple[str, str, str]] = []
        self.interrupts: list[dict[str, Any]] = []
        self.subagent_events: list[tuple[str, str]] = []
        self.rubric_events: list[dict[str, Any]] = []
        self.auto_mode_events: list[dict[str, Any]] = []
        self.started = False
        self.ended = False
        self.errors: list[Exception] = []

    async def on_stream_start(self, channel_id: str, thread_id: str) -> None:
        self.started = True

    async def on_token(self, token: str) -> None:
        self.tokens.append(token)

    async def on_thinking(self, text: str) -> None:
        self.thinking.append(text)

    async def on_tool_call_started(
        self, name: str, call_id: str, args: dict[str, Any]
    ) -> None:
        self.tool_starts.append((name, call_id, args))

    async def on_tool_call_completed(
        self, name: str, call_id: str, result: str
    ) -> None:
        self.tool_completes.append((name, call_id, result))

    async def on_interrupt(self, interrupt_value: dict[str, Any]) -> None:
        self.interrupts.append(interrupt_value)

    async def on_subagent_event(self, agent_name: str, status: str) -> None:
        self.subagent_events.append((agent_name, status))

    async def on_rubric_event(self, data: dict[str, Any]) -> None:
        self.rubric_events.append(data)

    async def on_auto_mode_event(self, data: dict[str, Any]) -> None:
        self.auto_mode_events.append(data)

    async def on_stream_end(self) -> None:
        self.ended = True

    async def on_error(self, error: Exception) -> None:
        self.errors.append(error)

    async def on_pre_interrupt(self) -> None:
        pass


def test_humanize_tool_name() -> None:
    """Verify tool names are formatted cleanly."""
    assert _humanize_tool_name("kubernetes_get_pods") == "Get Pods"
    assert _humanize_tool_name("helm_install_chart") == "Install Chart"
    assert _humanize_tool_name("transfer_to_k8s_operator") == "K8S Operator"


def test_extract_text_and_thinking_deepseek_anthropic() -> None:
    """Verify reasoning_content / thinking attributes are extracted."""
    # DeepSeek reasoning_content
    msg = AIMessage(content="Final Answer")
    setattr(msg, "reasoning_content", "DeepSeek reasoning step")
    text, thinking = _extract_text_and_thinking(msg.content, msg_obj=msg)
    assert text == "Final Answer"
    assert thinking == "DeepSeek reasoning step"

    # Anthropic / OpenAI additional_kwargs
    text, thinking = _extract_text_and_thinking(
        "Anthropic Answer",
        additional_kwargs={"thinking": "Anthropic reasoning trace"},
    )
    assert text == "Anthropic Answer"
    assert thinking == "Anthropic reasoning trace"

    # Inline XML thinking
    text, thinking = _extract_text_and_thinking(
        "<thinking>Planning steps</thinking>Here is the plan."
    )
    assert text == "Here is the plan."
    assert thinking == "Planning steps"


def test_extract_text_formatted_prefix() -> None:
    """Verify _extract_text formats thinking into markdown quote block."""
    result = _extract_text(
        "Response text",
        additional_kwargs={"thinking": "Reasoning trace"},
    )
    assert "> *Thinking:* Reasoning trace\n\nResponse text" == result


@pytest.mark.asyncio
async def test_graph_stream_bridge_processing() -> None:
    """Verify GraphStreamBridge dispatches messages, chunks, tools, and custom events."""
    sink = DummySink()
    bridge = GraphStreamBridge(sink)

    async def sample_stream():
        # Token chunks
        yield ("messages", (AIMessageChunk(content="Hello "), {}))
        yield (
            "messages",
            (
                AIMessageChunk(
                    content="World",
                    additional_kwargs={"thinking": "analyzing request"},
                ),
                {},
            ),
        )
        # Tool completion
        yield (
            "messages",
            ToolMessage(
                content="pod/nginx created",
                name="kubectl_apply",
                tool_call_id="call_123",
            ),
        )
        # Control message (should be skipped)
        yield ("messages", build_goal_state_notice({"goal_objective": "test"}))
        # Custom events
        yield ("custom", {"type": "subagent", "name": "k8s-operator", "status": "running"})
        yield ("custom", {"type": "auto_mode", "action": "approved"})

    accumulated = await bridge.process_stream(sample_stream())
    assert accumulated == "Hello World"
    assert sink.started is True
    assert sink.ended is True
    assert sink.tokens == ["Hello ", "World"]
    assert sink.thinking == ["analyzing request"]
    assert sink.tool_completes == [("kubectl_apply", "call_123", "pod/nginx created")]
    assert sink.subagent_events == [("k8s-operator", "running")]
    assert len(sink.auto_mode_events) == 1
