"""Unit tests for the K8s Autopilot Deep Agent Factory (Phase 1)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.pregel import Pregel

from k8s_autopilot.agent.config import AgentContext
from k8s_autopilot.agent.factory import (
    _build_hitl_interrupt_config,
    create_k8s_autopilot_agent,
)
from k8s_autopilot.backend.composite import K8sCompositeBackend


@tool
def sample_k8s_custom_tool(namespace: str = "default") -> str:
    """Sample custom tool."""
    return f"Namespace: {namespace}"


def test_agent_context_defaults() -> None:
    """Verify AgentContext dataclass defaults."""
    ctx = AgentContext()
    assert ctx.model == "gemini-3.7-flash"
    assert ctx.approval_mode == "manual"
    assert ctx.interactive is True
    assert ctx.auto_approve is False


def test_build_hitl_interrupt_config() -> None:
    """Verify HITL interrupt configuration in auto and yolo modes."""
    # YOLO mode (auto_approve=True) -> no interrupts
    yolo_cfg = _build_hitl_interrupt_config(auto_approve=True)
    assert yolo_cfg == {}

    # Auto / safe mode (auto_approve=False) -> mutating tools interrupt
    auto_cfg = _build_hitl_interrupt_config(auto_approve=False)
    assert auto_cfg["execute"] is True
    assert auto_cfg["delete_file"] is True
    assert auto_cfg["write_file"] is True


def test_create_k8s_autopilot_agent_lifecycle(tmp_path: Path) -> None:
    """Verify full deep agent creation and compilation with fake model and checkpointer."""
    fake_model = GenericFakeChatModel(messages=iter([]))
    checkpointer = MemorySaver()

    with patch("k8s_autopilot.agent.factory.create_model") as mock_cm:
        mock_res = MagicMock()
        mock_res.model = fake_model
        mock_cm.return_value = mock_res

        graph, backend = create_k8s_autopilot_agent(
            model="gemini-2.5-pro",
            assistant_id="k8s-autopilot",
            tools=[sample_k8s_custom_tool],
            checkpointer=checkpointer,
            cwd=tmp_path,
            interactive=True,
            auto_approve=False,
        )

        assert isinstance(graph, Pregel)
        assert isinstance(backend, K8sCompositeBackend)
        assert graph.checkpointer is not None


def test_create_k8s_autopilot_agent_with_chat_model_instance(tmp_path: Path) -> None:
    """Verify agent creation when passing a BaseChatModel instance directly."""
    fake_model = GenericFakeChatModel(messages=iter([]))

    graph, backend = create_k8s_autopilot_agent(
        model=fake_model,
        cwd=tmp_path,
        interactive=False,
        auto_approve=True,
    )

    assert isinstance(graph, Pregel)
    assert isinstance(backend, K8sCompositeBackend)


def test_create_k8s_autopilot_agent_default_tools_and_registry(tmp_path: Path) -> None:
    """Verify default catalog tools are dynamically attached via ToolRegistry."""
    fake_model = GenericFakeChatModel(messages=iter([]))

    with patch("k8s_autopilot.agent.factory.create_deep_agent") as mock_create_agent:
        mock_graph = MagicMock(spec=Pregel)
        mock_create_agent.return_value = mock_graph

        graph, backend = create_k8s_autopilot_agent(
            model=fake_model,
            tools=[sample_k8s_custom_tool],
            cwd=tmp_path,
            interactive=True,
            auto_approve=False,
        )

        assert graph is mock_graph
        call_kwargs = mock_create_agent.call_args.kwargs
        attached_tools = call_kwargs["tools"]
        attached_tool_names = {getattr(t, "name", None) for t in attached_tools}

        assert "sample_k8s_custom_tool" in attached_tool_names
        assert "web_search" in attached_tool_names
        assert "fetch_url" in attached_tool_names

