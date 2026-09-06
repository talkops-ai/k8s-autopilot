"""Integration tests for Wave 3 middleware interactions.

These tests verify that Wave 3 middleware components work together correctly
when composed in a stack, simulating realistic middleware pipeline scenarios.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ============================================================================
# Goal System + Ask User Integration
# ============================================================================


class TestGoalAskUserIntegration:
    """Verify that goal system and ask_user work together.

    In production, the ask_user tool is used to ask clarifying questions about
    goal completion evidence before the goal is marked complete.
    """

    def test_goal_tools_registered_with_correct_names(self):
        """Verify goal tools register with expected names."""
        from k8s_autopilot.middleware.goal_tools import GoalToolsMiddleware
        from k8s_autopilot.middleware.ask_user import AskUserMiddleware

        goal_mw = GoalToolsMiddleware()
        ask_mw = AskUserMiddleware()

        goal_tool_names = {t.name for t in goal_mw.tools}
        ask_tool_names = {t.name for t in ask_mw.tools}

        # No name collisions
        assert goal_tool_names.isdisjoint(ask_tool_names)
        assert goal_tool_names == {"get_goal", "get_rubric", "update_goal", "propose_goal"}
        assert ask_tool_names == {"ask_user"}

    def test_goal_notice_survives_with_ask_user(self):
        """Ensure goal notice generation works with ask_user middleware state."""
        from k8s_autopilot.middleware.goal_tools import GoalToolsMiddleware

        goal_mw = GoalToolsMiddleware()
        state = {
            "messages": [],
            "_goal_objective": "Deploy K8s service",
            "_goal_status": "active",
            "_goal_rubric": "Zero-downtime deployment",
        }
        # Notice should be generated
        result = goal_mw._notice_update(state)
        assert result is not None
        assert "messages" in result

        # Second call with notice already in messages should skip
        updated_state = dict(state)
        updated_state["messages"] = result["messages"]
        result2 = goal_mw._notice_update(updated_state)
        assert result2 is None


# ============================================================================
# HITL + Goal State Integration
# ============================================================================


class TestHITLGoalIntegration:
    """Verify HITL classifier context includes goal directives."""

    def test_active_user_directives_with_goal(self):
        """HITL classifier should see active goal directives."""
        from k8s_autopilot.middleware.auto_mode_hitl import _active_user_directives

        state = {
            "_goal_objective": "Fix CVE-2024-1234",
            "_goal_status": "active",
            "_goal_rubric": "Patch applied and tests passing",
        }
        directives = _active_user_directives(state)
        assert directives.get("goal_objective") == "Fix CVE-2024-1234"
        assert directives.get("goal_criteria") == "Patch applied and tests passing"

    def test_no_directives_without_goal(self):
        """HITL classifier context should be empty without active goal."""
        from k8s_autopilot.middleware.auto_mode_hitl import _active_user_directives

        directives = _active_user_directives({})
        assert directives == {}

    def test_paused_goal_not_in_directives(self):
        """Paused goals should not appear as active directives."""
        from k8s_autopilot.middleware.auto_mode_hitl import _active_user_directives

        state = {
            "_goal_objective": "Build feature",
            "_goal_status": "paused",
            "_goal_rubric": "All tests pass",
        }
        directives = _active_user_directives(state)
        assert directives.get("goal_objective") is None


# ============================================================================
# Subagents + Middleware Stack Integration
# ============================================================================


class TestSubagentsStackIntegration:
    """Verify subagents middleware integrates with the registry."""

    def test_subagents_registered_in_middleware_registry(self):
        """SubagentsMiddleware should be discoverable via the registry."""
        from k8s_autopilot.middleware.registry import get_middleware_registry
        # Force import to trigger registration
        import k8s_autopilot.middleware.subagents  # noqa: F401

        registry = get_middleware_registry()
        cls = registry.get("subagents")
        assert cls is not None
        assert cls.__name__ == "SubagentsMiddleware"

    def test_subagents_prompt_injection_format(self):
        """Verify the prompt block structure for built-in vs plugin subagents."""
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        metas = [
            {
                "name": "code-review",
                "description": "Review code changes",
                "system_prompt": "...",
                "source": "project",
                "path": "/agents/cr/AGENTS.md",
            },
            {
                "name": "analytics@plugin-a",
                "description": "Analytics agent",
                "system_prompt": "...",
                "source": "plugin",
                "path": "/plugins/a/agents/analytics/AGENTS.md",
                "is_plugin": True,
            },
        ]
        mw = SubagentsMiddleware(subagent_metas=metas)
        block = mw._build_prompt_block()

        assert "Built-in Subagents" in block
        assert "code-review" in block
        assert "Plugin & Extension Subagents" in block
        assert "analytics@plugin-a" in block
        assert "Automatic Routing Rules" in block


# ============================================================================
# Reasoning + Model Config Integration
# ============================================================================


class TestReasoningModelConfigIntegration:
    """Verify reasoning effort integrates with model config."""

    def test_model_spec_parse_feeds_reasoning(self):
        """ModelSpec.try_parse results feed correctly into reasoning functions."""
        from k8s_autopilot.model.config import ModelSpec
        from k8s_autopilot.model.reasoning import with_effort_model_params

        spec = ModelSpec.try_parse("google_genai:gemini-2.5-pro")
        assert spec is not None

        result = with_effort_model_params(
            f"{spec.provider}:{spec.model}", None, "high"
        )
        assert result["reasoning_effort"] == "high"
        assert result["thinking_level"] == "high"

    def test_effort_round_trip_all_providers(self):
        """Verify set → read → remove for all supported providers."""
        from k8s_autopilot.model.reasoning import (
            with_effort_model_params,
            current_effort_from_model_params,
            without_effort_model_params,
        )

        specs = [
            "google_genai:gemini-2.5-pro",
            "anthropic:claude-sonnet-4",
            "openai:o3",
        ]
        for spec in specs:
            params = with_effort_model_params(spec, None, "medium")
            read = current_effort_from_model_params(spec, params)
            assert read is not None, f"Failed to read effort for {spec}"

            cleaned = without_effort_model_params(spec, params)
            if cleaned:
                assert "reasoning_effort" not in cleaned


# ============================================================================
# HITL Deterministic Rules Integration
# ============================================================================


class TestHITLDeterministicRulesIntegration:
    """Integration tests for HITL deterministic allow/deny rules."""

    @pytest.fixture
    def worktree(self, tmp_path):
        """Create a realistic worktree structure."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test(): pass")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".env").write_text("SECRET=value")
        return tmp_path

    def test_routine_source_edits_allowed(self, worktree):
        """Routine source code edits within worktree should be allowed."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        extensions = [".py", ".ts", ".js", ".go", ".rs", ".java", ".md", ".yaml"]
        for ext in extensions:
            call = {
                "name": "write_file",
                "args": {"file_path": str(worktree / f"src/code{ext}")},
            }
            assert _deterministic_allow(worktree, call, None) is True, (
                f"Expected allow for {ext}"
            )

    def test_sensitive_paths_denied(self, worktree):
        """Writes to sensitive paths should be denied."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        sensitive_files = [".env", "deploy.sh", ".bashrc"]
        for name in sensitive_files:
            call = {
                "name": "write_file",
                "args": {"file_path": str(worktree / name)},
            }
            assert _deterministic_allow(worktree, call, None) is False, (
                f"Expected deny for {name}"
            )

    def test_git_read_commands_allowed(self, worktree):
        """Read-only git commands should be allowed."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        commands = ["git status", "git diff", "git log", "git show HEAD"]
        for cmd in commands:
            call = {"name": "execute", "args": {"command": cmd}}
            assert _deterministic_allow(worktree, call, None) is True, (
                f"Expected allow for '{cmd}'"
            )

    def test_git_write_commands_denied(self, worktree):
        """Write git commands should be denied."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        commands = [
            "git push origin main",
            "git commit -m 'test'",
            "git checkout -b new-branch",
        ]
        for cmd in commands:
            call = {"name": "execute", "args": {"command": cmd}}
            assert _deterministic_allow(worktree, call, None) is False, (
                f"Expected deny for '{cmd}'"
            )

    def test_dependency_file_writes_denied(self, worktree):
        """Dependency file writes should be denied by deterministic rules."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        dep_files = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml"]
        for name in dep_files:
            call = {
                "name": "write_file",
                "args": {"file_path": str(worktree / name)},
            }
            assert _deterministic_allow(worktree, call, None) is False, (
                f"Expected deny for {name}"
            )
