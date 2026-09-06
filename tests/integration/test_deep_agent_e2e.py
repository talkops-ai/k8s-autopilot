"""End-to-End Integration tests for K8s Autopilot Deep Agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
from k8s_autopilot.backend.composite import K8sCompositeBackend
from k8s_autopilot.integrations.stream_bridge import GraphStreamBridge


class MockEventCollector:
    """Collects stream bridge callbacks during end-to-end test execution."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.thinking: list[str] = []
        self.tool_calls: list[str] = []

    async def on_stream_start(self, channel_id: str, thread_id: str) -> None:
        pass

    async def on_token(self, token: str) -> None:
        self.tokens.append(token)

    async def on_thinking(self, text: str) -> None:
        self.thinking.append(text)

    async def on_tool_call_started(self, name: str, call_id: str, args: dict) -> None:
        self.tool_calls.append(name)

    async def on_tool_call_completed(self, name: str, call_id: str, result: str) -> None:
        pass

    async def on_interrupt(self, interrupt_value: dict) -> None:
        pass

    async def on_subagent_event(self, agent_name: str, status: str) -> None:
        pass

    async def on_rubric_event(self, data: dict) -> None:
        pass

    async def on_auto_mode_event(self, data: dict) -> None:
        pass

    async def on_stream_end(self) -> None:
        pass

    async def on_error(self, error: Exception) -> None:
        pass

    async def on_pre_interrupt(self) -> None:
        pass


@pytest.mark.asyncio
async def test_deep_agent_e2e_creation_and_streaming(tmp_path: Path) -> None:
    """Verify deep agent creation, backend binding, and stream event processing."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    fake_chat = GenericFakeChatModel(messages=iter([]))

    with patch("k8s_autopilot.agent.factory.create_model") as mock_cm:
        mock_res = MagicMock()
        mock_res.model = fake_chat
        mock_res.model_name = "test-k8s-agent"
        mock_cm.return_value = mock_res

        checkpointer = MemorySaver()
        graph, backend = create_k8s_autopilot_agent(
            model="gemini-2.5-pro",
            checkpointer=checkpointer,
            cwd=tmp_path,
            auto_approve=True,
        )

        assert isinstance(backend, K8sCompositeBackend)
        assert graph.checkpointer is not None

        # Verify stream bridge integration
        collector = MockEventCollector()
        bridge = GraphStreamBridge(collector)

        from langchain_core.messages import AIMessageChunk

        async def sample_graph_events():
            yield ("messages", (AIMessageChunk(content="Cluster is healthy."), {}))

        result = await bridge.process_stream(sample_graph_events())
        assert result == "Cluster is healthy."
        assert "Cluster is healthy." in collector.tokens
