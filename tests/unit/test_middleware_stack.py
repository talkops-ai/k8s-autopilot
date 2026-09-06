"""Unit tests for the middleware stack and factory assembly order."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage

from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
from k8s_autopilot.middleware.compaction import CLICompactionMiddleware
from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware
from k8s_autopilot.middleware.cost_tracking import CostTrackingMiddleware
from k8s_autopilot.middleware.goal_criteria import GoalCriteriaMiddleware
from k8s_autopilot.middleware.goal_tools import GoalToolsMiddleware
from k8s_autopilot.middleware.local_context import LocalContextMiddleware
from k8s_autopilot.middleware.memory_guard import ManagedMemoryGuardMiddleware
from k8s_autopilot.middleware.reliable_rubric import ReliableRubricMiddleware
from k8s_autopilot.middleware.resume_state import ResumeStateMiddleware
from k8s_autopilot.middleware.server_hooks import ServerHooksMiddleware
from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
from k8s_autopilot.middleware.subagents import SubagentsMiddleware
from k8s_autopilot.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
)


class TestMiddlewareRegistry:
    """Tests for MiddlewareRegistry singleton and build_stack."""

    def test_singleton(self) -> None:
        from k8s_autopilot.middleware.registry import get_middleware_registry

        r1 = get_middleware_registry()
        r2 = get_middleware_registry()
        assert r1 is r2

    def test_register_and_get(self) -> None:
        from k8s_autopilot.middleware.registry import MiddlewareRegistry

        registry = MiddlewareRegistry()
        mock_cls = MagicMock()
        registry.register("test_mw", mock_cls)
        assert registry.get("test_mw") is mock_cls

    def test_get_returns_none_for_unknown(self) -> None:
        from k8s_autopilot.middleware.registry import MiddlewareRegistry

        registry = MiddlewareRegistry()
        assert registry.get("nonexistent") is None

    def test_list_registered(self) -> None:
        from k8s_autopilot.middleware.registry import MiddlewareRegistry

        registry = MiddlewareRegistry()
        mock_a = MagicMock()
        mock_b = MagicMock()
        registry.register("beta", mock_b)
        registry.register("alpha", mock_a)
        assert registry.list_registered() == ["alpha", "beta"]

    def test_build_stack_preserves_order(self) -> None:
        from k8s_autopilot.middleware.registry import MiddlewareRegistry

        registry = MiddlewareRegistry()

        class MW1:
            pass

        class MW2:
            pass

        class MW3:
            pass

        registry.register("first", MW1)
        registry.register("second", MW2)
        registry.register("third", MW3)

        stack = registry.build_stack()
        assert len(stack) == 3
        assert isinstance(stack[0], MW1)
        assert isinstance(stack[1], MW2)
        assert isinstance(stack[2], MW3)

    def test_build_stack_excludes(self) -> None:
        from k8s_autopilot.middleware.registry import MiddlewareRegistry

        registry = MiddlewareRegistry()

        class MW1:
            pass

        class MW2:
            pass

        registry.register("keep", MW1)
        registry.register("skip", MW2)

        stack = registry.build_stack(exclude={"skip"})
        assert len(stack) == 1
        assert isinstance(stack[0], MW1)

    def test_decorator_registers(self) -> None:
        from k8s_autopilot.middleware.registry import get_middleware_registry

        registry = get_middleware_registry()
        assert "resume_state" in registry.list_registered()
        assert "unified_system_message" in registry.list_registered()
        assert "cost_tracking" in registry.list_registered()
        assert "goal_criteria" in registry.list_registered()
        assert "server_hooks" in registry.list_registered()


class TestUnifiedSystemMessage:
    """Tests for the unified system message middleware."""

    def test_string_content_passthrough(self) -> None:
        from k8s_autopilot.middleware.unified_system_message import unify_system_message

        msg = SystemMessage(content="hello world")
        result = unify_system_message(msg)
        assert result is msg

    def test_list_content_unified(self) -> None:
        from k8s_autopilot.middleware.unified_system_message import unify_system_message

        msg = SystemMessage(
            content=[
                {"type": "text", "text": "Part 1. "},
                {"type": "text", "text": "Part 2."},
            ]
        )
        result = unify_system_message(msg)
        assert result is not None
        assert isinstance(result.content, str)
        assert result.content == "Part 1. Part 2."

    def test_none_returns_none(self) -> None:
        from k8s_autopilot.middleware.unified_system_message import unify_system_message

        assert unify_system_message(None) is None


class TestResumeState:
    """Tests for the resume state middleware."""

    def test_extract_context_tokens(self) -> None:
        from k8s_autopilot.middleware.resume_state import _extract_context_tokens

        msg = AIMessage(
            content="test",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )
        assert _extract_context_tokens(msg) == 150

    def test_coerce_goal_status(self) -> None:
        from k8s_autopilot.middleware.resume_state import coerce_goal_status

        assert coerce_goal_status("active") == "active"
        assert coerce_goal_status("complete") == "complete"
        assert coerce_goal_status("invalid") is None

    def test_coerce_goal_proposal_kind(self) -> None:
        from k8s_autopilot.middleware.resume_state import coerce_goal_proposal_kind

        assert coerce_goal_proposal_kind("create") == "create"
        assert coerce_goal_proposal_kind("amend") == "amend"
        assert coerce_goal_proposal_kind("invalid") is None


class TestGlmStallRecovery:
    """Tests for GLM stall recovery middleware."""

    def test_model_detection(self) -> None:
        from k8s_autopilot.middleware.glm_stall_recovery import _is_fireworks_glm_5p2_model

        class FakeModel:
            model_name = "accounts/fireworks/models/glm-5.2-flash"

        assert _is_fireworks_glm_5p2_model(FakeModel())

    def test_non_glm_model(self) -> None:
        from k8s_autopilot.middleware.glm_stall_recovery import _is_fireworks_glm_5p2_model

        class FakeModel:
            model_name = "gpt-4o"

        assert not _is_fireworks_glm_5p2_model(FakeModel())


class TestFactoryMiddlewareOrder:
    """Verify that create_k8s_autopilot_agent wires all middlewares in the exact sequence."""

    def test_factory_middleware_order(self, tmp_path: Path) -> None:
        # Create dummy AGENTS.md so memory middleware attaches
        agents_md = tmp_path / ".agents" / "AGENTS.md"
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        agents_md.write_text("Test memory")

        # Create dummy skill so skills middleware attaches
        skill_dir = tmp_path / "skills" / "k8s_skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("---\nname: k8s_skill\n---\n")

        fake_model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))

        with patch("k8s_autopilot.agent.factory.create_deep_agent") as mock_create_agent:
            mock_create_agent.return_value = MagicMock()

            graph, backend = create_k8s_autopilot_agent(
                model=fake_model,
                cwd=tmp_path,
                interactive=True,
                enable_ask_user=True,
                enable_interpreter=True,
            )

            assert mock_create_agent.called
            passed_middleware = mock_create_agent.call_args[1]["middleware"]
            mw_types = [type(mw).__name__ for mw in passed_middleware]

            # Verify order of key middlewares:
            # 1. ConfigurableModelMiddleware
            # 2. ResumeStateMiddleware, CostTrackingMiddleware, GoalToolsMiddleware
            # 3. AskUserMiddleware
            # 4. MemoryMiddleware, ManagedMemoryGuardMiddleware
            # 5. PluginSkillsMiddleware
            # 6. CodeInterpreterMiddleware
            # 7. LocalContextMiddleware
            # 8. ServerHooksMiddleware
            # 9. GoalCriteriaMiddleware
            # 10. CLICompactionMiddleware
            # 11. ReliableRubricMiddleware
            # 12. SubagentsMiddleware
            # 13. UnifiedSystemMessageMiddleware

            assert "ConfigurableModelMiddleware" in mw_types
            assert "CostTrackingMiddleware" in mw_types
            assert "GoalToolsMiddleware" in mw_types
            assert "AskUserMiddleware" in mw_types
            assert "MemoryMiddleware" in mw_types
            assert "PluginSkillsMiddleware" in mw_types
            assert "LocalContextMiddleware" in mw_types
            assert "ServerHooksMiddleware" in mw_types
            assert "GoalCriteriaMiddleware" in mw_types
            assert "CLICompactionMiddleware" in mw_types
            assert "ReliableRubricMiddleware" in mw_types
            assert "SubagentsMiddleware" in mw_types
            assert "UnifiedSystemMessageMiddleware" in mw_types

            # Verify relative order of execution
            idx_cost = mw_types.index("CostTrackingMiddleware")
            idx_memory = mw_types.index("MemoryMiddleware")
            idx_skills = mw_types.index("PluginSkillsMiddleware")
            idx_local = mw_types.index("LocalContextMiddleware")
            idx_hooks = mw_types.index("ServerHooksMiddleware")
            idx_criteria = mw_types.index("GoalCriteriaMiddleware")
            idx_compact = mw_types.index("CLICompactionMiddleware")
            idx_rubric = mw_types.index("ReliableRubricMiddleware")
            idx_subagents = mw_types.index("SubagentsMiddleware")
            idx_unified = mw_types.index("UnifiedSystemMessageMiddleware")

            assert idx_cost < idx_memory < idx_skills < idx_local < idx_hooks < idx_criteria < idx_compact < idx_rubric < idx_subagents < idx_unified

    def test_awrap_model_call_middleware_order(self, tmp_path: Path) -> None:
        """Verify the exact order of middleware wrapping model calls matches OpsCode."""
        import deepagents.graph
        import langchain.agents.middleware.types

        # Create dummy AGENTS.md so memory middleware attaches
        agents_md = tmp_path / ".agents" / "AGENTS.md"
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        agents_md.write_text("Test memory")

        # Create dummy skill so skills middleware attaches
        skill_dir = tmp_path / "skills" / "k8s_skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("---\nname: k8s_skill\n---\n")

        class FakeChatModelWithTools(GenericFakeChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        fake_model = FakeChatModelWithTools(messages=iter([]))

        captured_middlewares: list[Any] = []
        orig_create_agent = deepagents.graph.create_agent

        def mock_create_agent(*args: Any, **kwargs: Any) -> Any:
            nonlocal captured_middlewares
            captured_middlewares = list(kwargs.get("middleware", []))
            return orig_create_agent(*args, **kwargs)

        with patch("deepagents.graph.create_agent", side_effect=mock_create_agent):
            create_k8s_autopilot_agent(
                model=fake_model,
                cwd=tmp_path,
                interactive=True,
                enable_ask_user=True,
                enable_interpreter=True,
            )

        model_wrapping_middlewares = [
            mw
            for mw in captured_middlewares
            if (
                type(mw).wrap_model_call
                != langchain.agents.middleware.types.AgentMiddleware.wrap_model_call
                or type(mw).awrap_model_call
                != langchain.agents.middleware.types.AgentMiddleware.awrap_model_call
            )
        ]

        model_wrapping_types = [type(mw).__name__ for mw in model_wrapping_middlewares]
        model_wrapping_names = [getattr(mw, "name", type(mw).__name__) for mw in model_wrapping_middlewares]

        expected_types = [
            "FilesystemMiddleware",
            "SubAgentMiddleware",
            "CLICompactionMiddleware",
            "ConfigurableModelMiddleware",
            "GoalToolsMiddleware",
            "AskUserMiddleware",
            "MemoryMiddleware",
            "PluginSkillsMiddleware",
            "CodeInterpreterMiddleware",
            "LocalContextMiddleware",
            "AutoModeHITLMiddleware",
            "SubagentsMiddleware",
            "UnifiedSystemMessageMiddleware",
            "AnthropicPromptCachingMiddleware",
        ]

        expected_names = [
            "FilesystemMiddleware",
            "SubAgentMiddleware",
            "SummarizationMiddleware",
            "ConfigurableModelMiddleware",
            "GoalToolsMiddleware",
            "AskUserMiddleware",
            "MemoryMiddleware",
            "PluginSkillsMiddleware",
            "CodeInterpreterMiddleware",
            "LocalContextMiddleware",
            "HumanInTheLoopMiddleware",
            "SubagentsMiddleware",
            "UnifiedSystemMessageMiddleware",
            "AnthropicPromptCachingMiddleware",
        ]

        # Verify exact sequence for all expected model wrappers
        for i, expected_type in enumerate(expected_types):
            assert model_wrapping_types[i] == expected_type
            assert model_wrapping_names[i] == expected_names[i]

        # Verify excluded from model wrapping
        assert "TodoListMiddleware" not in model_wrapping_types


