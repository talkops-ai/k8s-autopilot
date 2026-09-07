"""End-to-end verification of System Prompt & Context Architecture parity with OpsCode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
from k8s_autopilot.middleware.subagents import SubagentsMiddleware
from k8s_autopilot.skills.registry import SkillRegistry


class FakeChatModelWithTools(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_system_prompt_and_context_architecture_parity(tmp_path: Path) -> None:
    """Verify that K8s Autopilot satisfies all 5 core architecture invariants."""
    fake_model = FakeChatModelWithTools(messages=iter([AIMessage(content="ok")]))

    # 1. Skill Sources Check: Subagents MUST NOT leak into root SkillRegistry sources
    skill_registry = SkillRegistry.get_instance()
    skill_sources = skill_registry.get_sources_for_middleware()
    source_labels = [s[1] for s in skill_sources]
    assert "Built-in" in source_labels
    assert not any("Subagent:" in label for label in source_labels)

    # 2. Live Skills Check: Root skills contain only project/user/plugin skills (no redundant built-ins or subagent leaks)
    skills_mw = PluginSkillsMiddleware()
    live_skills, _ = skills_mw._get_live_skills()
    live_names = {s["name"] for s in live_skills}

    assert "remember" not in live_names
    assert "kubernetes" not in live_names
    assert not any(name.startswith("app-operator:") for name in live_names)
    assert not any(name.startswith("helm-operator:") for name in live_names)
    assert not any(name.startswith("k8s-operator:") for name in live_names)
    assert not any(name.startswith("observability-operator:") for name in live_names)

    # 3. Progressive Disclosure Check: Paths point to SKILL.md
    for skill in live_skills:
        assert str(skill["path"]).endswith("SKILL.md")
        assert Path(skill["path"]).is_file()

    # 4. Agent Creation & Subagents Middleware Check
    with patch("k8s_autopilot.agent.factory.create_deep_agent") as mock_create_agent:
        mock_create_agent.return_value = MagicMock()

        graph, backend = create_k8s_autopilot_agent(
            model=fake_model,
            cwd=tmp_path,
            interactive=True,
            enable_interpreter=True,
        )

        assert mock_create_agent.called
        call_kwargs = mock_create_agent.call_args[1]

        # 4a. Compiled subagents passed to create_deep_agent
        subagents = call_kwargs.get("subagents", [])
        subagent_names = {s["name"] for s in subagents}
        assert "app-operator" in subagent_names
        assert "helm-operator" in subagent_names
        assert "k8s-operator" in subagent_names
        assert "observability-operator" in subagent_names
        assert "general-purpose" in subagent_names

        # 4b. SubagentsMiddleware Prompt Block Check
        middlewares = call_kwargs["middleware"]
        subagents_mw = next(mw for mw in middlewares if isinstance(mw, SubagentsMiddleware))
        prompt_block = subagents_mw._build_prompt_block()

        assert "## Subagent Delegation & Orchestration Architecture" in prompt_block
        assert "### 1. Built-in Subagents (Direct `task` Tool)" in prompt_block
        assert "app-operator" in prompt_block
        assert "helm-operator" in prompt_block
        assert "k8s-operator" in prompt_block
        assert "observability-operator" in prompt_block
        assert "Automatic Routing Rules" in prompt_block

        # 4c. CodeInterpreterMiddleware PTC Option Check
        from langchain_quickjs import CodeInterpreterMiddleware
        interpreter_mw = next(
            (mw for mw in middlewares if isinstance(mw, CodeInterpreterMiddleware)), None
        )
        if interpreter_mw is not None:
            assert getattr(interpreter_mw, "_ptc", None) == ["glob", "grep", "read_file"]
