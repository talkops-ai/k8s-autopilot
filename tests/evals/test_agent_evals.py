"""Agent evaluation test suite for K8s Autopilot Deep Agent scenarios."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
from k8s_autopilot.subagents.loader import list_subagents
from k8s_autopilot.subagents.subagents_parser import parse_built_in_subagents


def test_eval_subagent_registry_coverage() -> None:
    """Evaluate that all 4 domain subagents are discovered and have requisite skills."""
    built_in = parse_built_in_subagents()
    agents = {a["name"]: a for a in built_in}

    # 1. K8s Operator
    assert "k8s-operator" in agents
    assert "kubernetes-cluster-ops" in agents["k8s-operator"]["skills"]

    # 2. Helm Operator
    assert "helm-operator" in agents
    assert "helm-operation" in agents["helm-operator"]["skills"]

    # 3. App Operator
    assert "app-operator" in agents
    assert "argocd-gitops" in agents["app-operator"]["skills"]
    assert "argo-rollout-gitops" in agents["app-operator"]["skills"]

    # 4. Observability Operator
    assert "observability-operator" in agents
    assert "prometheus" in agents["observability-operator"]["skills"]
    assert "alertmanager" in agents["observability-operator"]["skills"]
    assert "loki" in agents["observability-operator"]["skills"]


def test_eval_agent_system_prompt_domain_readiness() -> None:
    """Evaluate system prompt generation contains core domain directives."""
    from k8s_autopilot.prompts import get_base_system_prompt

    prompt = get_base_system_prompt(model_name="gemini-2.5-pro")
    assert "Kubernetes" in prompt
    assert "K8s Autopilot" in prompt
    assert "gemini-2.5-pro" in prompt


@pytest.mark.asyncio
async def test_eval_k8s_diagnostic_flow(tmp_path: Path) -> None:
    """Evaluate agent structure on diagnostic command workflows."""
    fake_chat = GenericFakeChatModel(messages=iter([]))

    with patch("k8s_autopilot.agent.factory.create_model") as mock_cm:
        mock_res = MagicMock()
        mock_res.model = fake_chat
        mock_res.model_name = "eval-model"
        mock_cm.return_value = mock_res

        graph, backend = create_k8s_autopilot_agent(
            model=fake_chat,
            cwd=tmp_path,
            auto_approve=True,
            checkpointer=MemorySaver(),
        )

        assert graph is not None
        assert backend is not None
