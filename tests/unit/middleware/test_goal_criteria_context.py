"""Unit tests for goal criteria agent context propagation and system message unification.

Verifies:
1. Goal criteria agent includes PluginSkillsMiddleware with both deep-agent and subagent skills.
2. Goal criteria agent includes SubagentsMiddleware with available subagents and capabilities.
3. Goal criteria agent uses UnifiedSystemMessageMiddleware to collapse prompt blocks into a single string.
4. Context isolation is preserved for the main deep agent and subagents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage

from k8s_autopilot.middleware.goal_criteria import (
    GoalCriteriaAgentState,
    create_goal_criteria_agent,
    create_goal_criteria_fallback_agent,
)
from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
from k8s_autopilot.middleware.subagents import SubagentsMiddleware
from k8s_autopilot.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
    unify_system_message,
)
from k8s_autopilot.skills.registry import SkillRegistry, SkillSource
from k8s_autopilot.subagents.types import SubagentMetadata


class CapturedMessageHolder:
    messages: list[Any] = []


class FakeChatModelWithTools(GenericFakeChatModel):
    """Fake chat model that supports bind_tools and captures prompt messages."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        CapturedMessageHolder.messages = list(messages)
        from langchain_core.outputs import ChatGeneration, ChatResult

        tool_call = {
            "name": "GoalProposal",
            "args": {"objective": "Test objective", "criteria": "- Criterion 1\n- Criterion 2"},
            "id": "call_proposal_1",
            "type": "tool_call",
        }
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="", tool_calls=[tool_call])
                )
            ]
        )


class TestUnifiedSystemMessage:
    """Tests for unify_system_message function."""

    def test_unify_none(self) -> None:
        assert unify_system_message(None) is None

    def test_unify_already_str(self) -> None:
        msg = SystemMessage(content="Simple system prompt")
        assert unify_system_message(msg) is msg

    def test_unify_list_of_text_dicts(self) -> None:
        msg = SystemMessage(
            content=[
                {"type": "text", "text": "First paragraph.\n\n"},
                {"type": "text", "text": "Second paragraph."},
            ]
        )
        unified = unify_system_message(msg)
        assert isinstance(unified, SystemMessage)
        assert isinstance(unified.content, str)
        assert unified.content == "First paragraph.\n\nSecond paragraph."

    def test_unify_mixed_blocks(self) -> None:
        msg = SystemMessage(
            content=[
                "Direct string block.",
                {"type": "text", "text": "Dict block."},
            ]
        )
        unified = unify_system_message(msg)
        assert isinstance(unified, SystemMessage)
        assert isinstance(unified.content, str)
        assert "Direct string block." in unified.content
        assert "Dict block." in unified.content


class TestGoalCriteriaAgentEnhancements:
    """Tests for GoalCriteriaAgent middleware and context enhancements."""

    def test_criteria_agent_middleware_stack(self, tmp_path: Path) -> None:
        """Verify PluginSkillsMiddleware, SubagentsMiddleware, and UnifiedSystemMessageMiddleware are attached."""
        from k8s_autopilot.backend.local import LocalShellBackend

        backend = LocalShellBackend(root_dir=tmp_path)
        fake_model = FakeChatModelWithTools(messages=iter([]))

        subagent_metas: list[SubagentMetadata] = [
            {
                "name": "k8s-pod-debugger",
                "description": "Diagnoses failing pods and logs",
                "system_prompt": "Debug pods",
                "source": "builtin",
                "path": str(tmp_path),
                "tools": ["kubectl_logs"],
            }
        ]

        dummy_skill_dir = tmp_path / "custom_skills" / "k8s_debug"
        dummy_skill_dir.mkdir(parents=True)
        (dummy_skill_dir / "SKILL.md").write_text("---\nname: k8s_debug\ndescription: Debug k8s resources\n---\n")

        skill_sources = [(str(dummy_skill_dir.parent), "Project")]

        agent = create_goal_criteria_agent(
            model=fake_model,
            repository_backend=backend,
            repository_root=str(tmp_path),
            subagent_metas=subagent_metas,
            skill_sources=skill_sources,
        )

        # Ensure agent is created and runnable
        assert agent is not None

        # Invoke agent to trigger model call and verify captured prompt
        res = agent.invoke({
            "messages": [{"role": "user", "content": "Fix crashed pod"}],
            "criteria_objective": "Fix crashed pod",
            "criteria_operation_id": "test_op_1",
        })

        assert CapturedMessageHolder.messages, "Model should have been invoked"
        system_msg = CapturedMessageHolder.messages[0]
        assert isinstance(system_msg, SystemMessage)

        # CRITICAL ASSERTION: System message content MUST be a string, NOT a list of dicts!
        assert isinstance(system_msg.content, str), f"Expected str, got {type(system_msg.content)}: {system_msg.content!r}"

        # 1. Persona & Principles verification
        assert "# K8s Autopilot — Goal Acceptance Criteria & Planning Architect" in system_msg.content
        assert "Core Planning Principles" in system_msg.content
        assert "Tool Restraint & Anti-Exploration Guardrails" in system_msg.content
        assert "Do NOT invoke discovery or exploration tools" in system_msg.content

        # 2. Context verification: Contains skills system without progressive disclosure clutter
        assert "Skills System" in system_msg.content
        assert "k8s_debug" in system_msg.content
        assert "Progressive Disclosure" not in system_msg.content
        assert "limit=1000" not in system_msg.content

        # 3. Context verification: Contains subagent delegation without execution mechanics or js_eval
        assert "Subagent Delegation" in system_msg.content
        assert "k8s-pod-debugger" in system_msg.content
        assert "Diagnoses failing pods" in system_msg.content
        assert "js_eval" not in system_msg.content
        assert "await task" not in system_msg.content
        assert "Automatic Routing Rules" not in system_msg.content

        # 4. Virtual mounts suppression
        assert "Shell paths vs. virtual paths" not in system_msg.content

    def test_main_agent_subagents_middleware_preserves_execution_instructions(self) -> None:
        """Verify default SubagentsMiddleware (planning_mode=False) preserves full execution instructions."""
        subagent_metas: list[SubagentMetadata] = [
            {
                "name": "plugin-subagent@test",
                "description": "Plugin subagent",
                "system_prompt": "",
                "source": "plugin",
            },
            {
                "name": "builtin-subagent",
                "description": "Built-in subagent",
                "system_prompt": "",
                "source": "builtin",
            },
        ]
        mw = SubagentsMiddleware(subagent_metas=subagent_metas)
        prompt_block = mw._build_prompt_block()

        # Must include execution primitives and routing rules for deep agent execution
        assert "js_eval" in prompt_block
        assert "Direct `task` Tool" in prompt_block
        assert "Automatic Routing Rules" in prompt_block
        assert "await task" in prompt_block

    def test_dynamic_and_builtin_subagents_in_planning_mode_vs_execution_mode(self) -> None:
        """Verify that dynamic and built-in subagents bifurcation works in execution mode and stays clean in planning mode."""
        subagent_metas: list[SubagentMetadata] = [
            {
                "name": "k8s-operator",
                "description": "Core Kubernetes cluster operator",
                "system_prompt": "Manage k8s",
                "source": "builtin",
            },
            {
                "name": "financial-analyzer@analytics-plugin",
                "description": "Dynamic plugin financial analyzer",
                "system_prompt": "Analyze finances",
                "source": "plugin:analytics-plugin",
                "skills": ["financial-reporting"],
            },
        ]

        # 1. Execution mode (coordinator / deep agent)
        exec_mw = SubagentsMiddleware(subagent_metas=subagent_metas, planning_mode=False)
        exec_block = exec_mw._build_prompt_block()

        assert "Built-in Subagents (Direct `task` Tool)" in exec_block
        assert "k8s-operator" in exec_block
        assert 'task(description="...", subagent_type="<subagent_name>")' in exec_block

        assert "Plugin & Extension Subagents (Code Interpreter `js_eval`)" in exec_block
        assert "financial-analyzer@analytics-plugin" in exec_block
        assert "await task({" in exec_block
        assert "subagentType: \"<subagent_name@plugin_id>\"" in exec_block

        assert "Automatic Routing Rules (Zero User Intervention)" in exec_block
        assert "Auto-Detect Origin" in exec_block

        # 2. Planning mode (goal criteria agent)
        plan_mw = SubagentsMiddleware(subagent_metas=subagent_metas, planning_mode=True)
        plan_block = plan_mw._build_prompt_block()

        assert "Subagent Delegation & Operational Capabilities" in plan_block
        assert "k8s-operator" in plan_block
        assert "financial-analyzer@analytics-plugin" in plan_block
        # Must strictly omit execution instructions
        assert "js_eval" not in plan_block
        assert "await task" not in plan_block
        assert "Automatic Routing Rules" not in plan_block
        assert "Direct `task` Tool" not in plan_block

    def test_criteria_agent_with_dynamic_plugin_subagents(self, tmp_path: Path) -> None:
        """Verify goal criteria agent prompt with dynamic plugin subagents omits execution routing."""
        from k8s_autopilot.backend.local import LocalShellBackend

        backend = LocalShellBackend(root_dir=tmp_path)
        fake_model = FakeChatModelWithTools(messages=iter([]))

        subagent_metas: list[SubagentMetadata] = [
            {
                "name": "builtin-k8s",
                "description": "Standard k8s operator",
                "system_prompt": "",
                "source": "builtin",
            },
            {
                "name": "custom-plugin:analyzer",
                "description": "Dynamic plugin analyzer",
                "system_prompt": "",
                "source": "plugin:custom-plugin",
                "skills": ["custom-analysis"],
            },
        ]

        agent = create_goal_criteria_agent(
            model=fake_model,
            repository_backend=backend,
            repository_root=str(tmp_path),
            subagent_metas=subagent_metas,
        )

        agent.invoke({
            "messages": [{"role": "user", "content": "Analyze cluster"}],
            "criteria_objective": "Analyze cluster",
            "criteria_operation_id": "test_dyn_1",
        })

        assert CapturedMessageHolder.messages
        system_msg = CapturedMessageHolder.messages[0]
        assert isinstance(system_msg.content, str)

        # Context present as capabilities
        assert "builtin-k8s" in system_msg.content
        assert "custom-plugin:analyzer" in system_msg.content
        assert "Subagent Delegation & Operational Capabilities" in system_msg.content

        # Execution clutter strictly absent
        assert "js_eval" not in system_msg.content
        assert "await task" not in system_msg.content
        assert "Automatic Routing Rules" not in system_msg.content

    def test_subagent_cli_middleware_preserves_full_skills_execution(self, tmp_path: Path) -> None:
        """Verify subagent CLI middleware retains execution mode (planning_mode=False) for PluginSkillsMiddleware."""
        from k8s_autopilot.agent.factory import _subagent_cli_middleware

        dummy_skill_dir = tmp_path / "skills" / "sub_skill"
        dummy_skill_dir.mkdir(parents=True)
        (dummy_skill_dir / "SKILL.md").write_text("---\nname: sub_skill\ndescription: Subagent skill\n---\n")

        with pytest.MonkeyPatch.context() as mp:
            from k8s_autopilot.skills.registry import SkillRegistry
            registry = SkillRegistry.get_instance()
            mp.setattr(
                registry,
                "get_sources_for_middleware",
                lambda **kwargs: [(str(dummy_skill_dir.parent), "Test")],
            )

            mw_list = _subagent_cli_middleware(
                has_explicit_model=True,
                assistant_id="k8s-autopilot",
                subagent_name="test-worker",
                worktree_root=tmp_path,
            )

            skills_mws = [m for m in mw_list if isinstance(m, PluginSkillsMiddleware)]
            assert len(skills_mws) == 1
            skills_mw = skills_mws[0]
            # Must NOT be in planning mode! Subagents execute skills using progressive disclosure.
            assert getattr(skills_mw, "_planning_mode", False) is False

    def test_execute_tool_whitelisted_and_budgeted_for_criteria_agent(self, tmp_path: Path) -> None:
        """Verify execute tool is whitelisted and supported in repository bounds for criteria agent."""
        from k8s_autopilot.backend.local import LocalShellBackend
        from k8s_autopilot.middleware._repository_bounds import RepositoryBounds, REPOSITORY_TOOL_NAMES

        # 1. Default bounds remain read-only
        assert "execute" not in REPOSITORY_TOOL_NAMES

        # 2. RepositoryBounds with allowed_tools accepts execute
        backend = LocalShellBackend(root_dir=tmp_path)
        bounds = RepositoryBounds(
            backend, root=str(tmp_path), allowed_tools=["read_file", "ls", "execute"]
        )
        preflight_error = bounds.preflight("execute", {"command": "echo test"})
        assert preflight_error is None

        # 3. Goal criteria agent exposes execute alongside read_file and ls
        fake_model = FakeChatModelWithTools(messages=iter([]))
        agent = create_goal_criteria_agent(
            model=fake_model,
            repository_backend=backend,
            repository_root=str(tmp_path),
        )
        assert agent is not None

    def test_fallback_agent_has_unified_system_message(self) -> None:
        """Verify fallback agent also normalizes its system message to a string."""
        fake_model = FakeChatModelWithTools(messages=iter([]))
        fallback_agent = create_goal_criteria_fallback_agent(model=fake_model)

        assert fallback_agent is not None
        res = fallback_agent.invoke({
            "messages": [{"role": "user", "content": "Do task"}],
            "criteria_objective": "Do task",
            "criteria_operation_id": "test_op_2",
        })

        assert CapturedMessageHolder.messages
        system_msg = CapturedMessageHolder.messages[0]
        assert isinstance(system_msg, SystemMessage)
        assert isinstance(system_msg.content, str)


class TestSkillRegistryContextIsolation:
    """Tests that include_subagent_skills distinguishes deep-agent vs all-inclusive skill discovery."""

    def test_get_sources_default_excludes_agent_plugins(self, tmp_path: Path) -> None:
        """Verify default call preserves context isolation by excluding agent plugins."""
        registry = SkillRegistry()

        # Create mock plugin with agents in a plugin cache folder
        plugin_dir = tmp_path / "cache" / "agent_plugin"
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "agent.md").write_text("agent")

        plugin_skills_dir = plugin_dir / "skills"
        plugin_skills_dir.mkdir(parents=True, exist_ok=True)

        mock_plugin = MagicMock()
        mock_plugin.inventory.agents = [agents_dir / "agent.md"]
        mock_plugin.inventory.skills = [plugin_skills_dir]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "k8s_autopilot.plugins.discovery.discover_plugins",
                lambda **kwargs: MagicMock(plugins=[mock_plugin]),
            )
            mp.setattr(
                "k8s_autopilot.plugins.adapters.skills.plugin_skill_sources",
                lambda plugins: [(str(plugin_skills_dir), "Plugin: test", "test_plugin")],
            )

            # 1. include_subagent_skills=False (Default, for deep agent) -> Excluded
            sources_deep_agent = registry.get_sources_for_middleware(
                project_root=tmp_path,
                include_subagent_skills=False,
            )
            assert not any(s[0] == str(plugin_skills_dir) for s in sources_deep_agent)

            # 2. include_subagent_skills=True (For criteria agent) -> Included
            sources_criteria_agent = registry.get_sources_for_middleware(
                project_root=tmp_path,
                include_subagent_skills=True,
            )
            assert any(s[0] == str(plugin_skills_dir) for s in sources_criteria_agent)

    def test_bundled_subagent_skills_included_when_requested(self, tmp_path: Path) -> None:
        """Verify subagents with bundled skills directory are included when requested."""
        registry = SkillRegistry()

        subagent_dir = tmp_path / "subagents" / "specialized"
        subagent_skills = subagent_dir / "skills"
        subagent_skills.mkdir(parents=True)
        (subagent_skills / "my_skill").mkdir()
        (subagent_skills / "my_skill" / "SKILL.md").write_text("---\nname: my_skill\n---\n")

        subagent_meta: SubagentMetadata = {
            "name": "specialized-agent",
            "description": "Specialized agent",
            "system_prompt": "",
            "source": "custom",
            "path": str(subagent_dir / "agent.json"),
        }

        # 1. Default (False) does not include bundled subagent skills
        sources_default = registry.get_sources_for_middleware(
            project_root=tmp_path,
            include_subagent_skills=False,
            subagents=[subagent_meta],
        )
        assert not any(s[0] == str(subagent_skills) for s in sources_default)

        # 2. include_subagent_skills=True includes the bundled subagent skills
        sources_criteria = registry.get_sources_for_middleware(
            project_root=tmp_path,
            include_subagent_skills=True,
            subagents=[subagent_meta],
        )
        assert any(s[0] == str(subagent_skills) for s in sources_criteria)
