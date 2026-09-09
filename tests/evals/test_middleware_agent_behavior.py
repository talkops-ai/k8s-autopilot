"""Agent evaluation tests for Wave 3 components.

These tests evaluate the behavioral correctness of Wave 3 middleware from an
agent perspective — testing that the middleware produces the correct outputs,
policy decisions, and state transitions that an agent would observe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ============================================================================
# Goal System Agent Evals
# ============================================================================


class TestGoalSystemAgentEvals:
    """Evaluate that goal tools produce correct agent-visible behavior."""

    def test_goal_lifecycle_active_to_blocked_to_complete(self):
        """Simulate goal lifecycle: active → blocked → unblock → complete."""
        from k8s_autopilot.tools.goal_tools import (
            _goal_snapshot,
            _rubric_snapshot,
            _update_goal_command,
        )

        # Step 1: Active goal
        state = {
            "_goal_objective": "Implement HITL middleware",
            "_goal_status": "active",
            "_goal_rubric": "All tests pass",
        }
        snap = _goal_snapshot(state)
        assert snap["active"] is True
        assert snap["status"] == "active"

        # Step 2: Agent reports blocked
        cmd = _update_goal_command(
            status="blocked",
            note="Missing dependency: pydantic",
            tool_call_id="tc-block",
            state=state,
        )
        state.update(cmd.update)
        snap = _goal_snapshot(state)
        assert snap["active"] is True
        assert snap["status"] == "blocked"
        assert snap["note"] == "Missing dependency: pydantic"

        # Step 3: User resolves blocker → status back to active
        state["_goal_status"] = "active"
        snap = _goal_snapshot(state)
        assert snap["active"] is True
        assert snap["status"] == "active"

        # Step 4: Agent stages completion
        cmd = _update_goal_command(
            status="complete",
            note="HITL middleware fully implemented with tests",
            tool_call_id="tc-complete",
            state=state,
        )
        assert cmd.update.get("_pending_goal_completion_note") == (
            "HITL middleware fully implemented with tests"
        )

    def test_goal_rubric_precedence_chain(self):
        """Evaluate rubric precedence: invocation > goal > sticky."""
        from k8s_autopilot.tools.goal_tools import _rubric_snapshot

        # Only sticky
        state = {"_sticky_rubric": "Sticky criteria"}
        snap = _rubric_snapshot(state)
        assert snap["criteria"] == "Sticky criteria"

        # Goal rubric overrides sticky (when goal is active)
        state["_goal_objective"] = "Test"
        state["_goal_status"] = "active"
        state["_goal_rubric"] = "Goal criteria"
        snap = _rubric_snapshot(state)
        assert snap["criteria"] == "Goal criteria"

        # Invocation rubric overrides both
        state["rubric"] = "Invocation criteria"
        snap = _rubric_snapshot(state)
        assert snap["criteria"] == "Invocation criteria"

    def test_notice_generation_correct_for_agent_context(self):
        """Verify goal state notices contain correct orientation for agents."""
        from k8s_autopilot.middleware.goal_state_notice import build_goal_state_notice

        # Active goal with rubric
        notice = build_goal_state_notice({
            "_goal_objective": "Build API",
            "_goal_status": "active",
            "_goal_rubric": "All endpoints tested",
        })
        assert "actionable: yes" in notice.content
        assert "Rubric active: yes" in notice.content
        assert "get_goal" in notice.content

        # No goal, no rubric
        notice = build_goal_state_notice({})
        assert "actionable: no" in notice.content


# ============================================================================
# HITL Safety Gate Agent Evals
# ============================================================================


class TestHITLSafetyGateEvals:
    """Evaluate that HITL safety gates make correct decisions from agent POV."""

    def test_safe_operations_auto_allowed(self, tmp_path):
        """Agent's routine operations should pass deterministic allow."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        root = tmp_path / "test-repo"
        root.mkdir()
        safe_ops = [
            {"name": "write_file", "args": {"file_path": str(root / "src" / "handler.py")}},
            {"name": "write_file", "args": {"file_path": str(root / "tests" / "test_handler.py")}},
            {"name": "write_file", "args": {"file_path": str(root / "README.md")}},
            {"name": "web_search", "args": {"query": "kubernetes RBAC"}},
            {"name": "execute", "args": {"command": "git status"}},
            {"name": "execute", "args": {"command": "git diff HEAD~1"}},
        ]
        for call in safe_ops:
            assert _deterministic_allow(root, call, None) is True, (
                f"Expected auto-allow for {call['name']} with {call.get('args', {})}"
            )

    def test_dangerous_operations_denied(self, tmp_path):
        """Agent's dangerous operations should be denied by deterministic rules."""
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        root = tmp_path / "test-repo"
        root.mkdir()
        dangerous_ops = [
            # Outside worktree
            {"name": "write_file", "args": {"file_path": "/etc/hosts"}},
            # Sensitive files
            {"name": "write_file", "args": {"file_path": str(root / ".env")}},
            {"name": "write_file", "args": {"file_path": str(root / ".ssh" / "config")}},
            # Shell scripts
            {"name": "write_file", "args": {"file_path": str(root / "deploy.sh")}},
            # Dangerous commands
            {"name": "execute", "args": {"command": "git push --force origin main"}},
            {"name": "execute", "args": {"command": "rm -rf /"}},
        ]
        for call in dangerous_ops:
            assert _deterministic_allow(root, call, None) is False, (
                f"Expected deny for {call['name']} with {call.get('args', {})}"
            )

    def test_classifier_model_schema_valid(self):
        """Verify AutoDecisionBatch produces valid structured output schema."""
        from k8s_autopilot.middleware.auto_mode_hitl import (
            AutoDecision,
            AutoDecisionBatch,
            AutoDecisionCategory,
        )

        # Simulate what the LLM classifier returns
        batch = AutoDecisionBatch(
            decisions=[
                AutoDecision(
                    tool_call_id="tc-1",
                    decision="allow",
                    category=AutoDecisionCategory.OTHER_POLICY,
                    reason="",
                ),
                AutoDecision(
                    tool_call_id="tc-2",
                    decision="deny",
                    category=AutoDecisionCategory.DESTRUCTIVE_ACTION,
                    reason="Deletes production database",
                ),
            ]
        )
        assert len(batch.decisions) == 2
        assert batch.decisions[0].decision == "allow"
        assert batch.decisions[1].decision == "deny"
        assert batch.decisions[1].reason == "Deletes production database"

    def test_classifier_policy_prompt_content(self):
        """Verify the classifier policy prompt contains essential safety rules."""
        from k8s_autopilot.middleware.auto_mode_hitl import _CLASSIFIER_POLICY

        # Must mention authorization evidence
        assert "authorization_evidence" in _CLASSIFIER_POLICY
        # Must mention trust boundary
        assert "trust boundary" in _CLASSIFIER_POLICY
        # Must mention force-push
        assert "force-push" in _CLASSIFIER_POLICY
        # Must mention credentials
        assert "credential" in _CLASSIFIER_POLICY.lower()
        # Must mention scope escalation
        assert "scope escalation" in _CLASSIFIER_POLICY


# ============================================================================
# Subagent Routing Agent Evals
# ============================================================================


class TestSubagentRoutingEvals:
    """Evaluate subagent routing from the agent's perspective."""

    def test_built_in_vs_plugin_routing_clarity(self):
        """Agent should see clear routing guidance in system prompt."""
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        mw = SubagentsMiddleware(subagent_metas=[
            {
                "name": "code-review",
                "description": "Reviews code",
                "system_prompt": "...",
                "source": "project",
                "path": "/agents/cr/AGENTS.md",
            },
            {
                "name": "docs@doc-plugin",
                "description": "Generates docs",
                "system_prompt": "...",
                "source": "plugin",
                "path": "/plugins/doc/agents/docs/AGENTS.md",
                "is_plugin": True,
            },
        ])
        block = mw._build_prompt_block()

        # Built-in should use task tool
        assert "task" in block.lower()
        # Plugin should use js_eval
        assert "js_eval" in block
        # Auto-routing rules present
        assert "Auto-Detect Origin" in block
        assert "Never Ask the User" in block

    def test_subagent_lookup_by_name(self):
        """Agent should be able to look up full subagent metadata by name."""
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        mw = SubagentsMiddleware(subagent_metas=[
            {
                "name": "k8s-deploy",
                "description": "Deploy to K8s",
                "system_prompt": "You deploy workloads to Kubernetes clusters.",
                "model": "google_genai:gemini-2.5-pro",
                "source": "project",
                "path": "/agents/deploy/AGENTS.md",
            },
        ])
        meta = mw.get_subagent("k8s-deploy")
        assert meta is not None
        assert meta["system_prompt"] == "You deploy workloads to Kubernetes clusters."
        assert meta["model"] == "google_genai:gemini-2.5-pro"

        # Unknown name returns None
        assert mw.get_subagent("nonexistent") is None


# ============================================================================
# Reasoning Effort Agent Evals
# ============================================================================


class TestReasoningEffortEvals:
    """Evaluate reasoning effort from agent's parameter construction perspective."""

    def test_effort_levels_match_model_capabilities(self):
        """Verify effort levels align with model profile definitions."""
        from k8s_autopilot.model.reasoning import (
            supported_efforts_for_model,
            is_effort_supported_for_model,
        )

        # Gemini 3.1 Pro supports low and high
        levels = supported_efforts_for_model("google_genai:gemini-3.1-pro")
        assert levels == ("low", "high")

        # Gemini 3.7 Flash supports low, medium, high
        levels = supported_efforts_for_model("google_genai:gemini-3.7-flash")
        assert levels == ("low", "medium", "high")

        # Unknown models return empty tuple (no profile)
        levels = supported_efforts_for_model("unknown:model-x")
        assert levels == ()

    def test_provider_native_params_correct(self):
        """Verify each provider gets canonical reasoning_effort without colliding keys."""
        from k8s_autopilot.model.reasoning import with_effort_model_params

        # Google: canonical flat reasoning_effort without legacy keys
        gemini = with_effort_model_params("google_genai:gemini-2.5-pro", None, "high")
        assert gemini["reasoning_effort"] == "high"
        assert "thinking_level" not in gemini
        assert "thinking_budget" not in gemini

        # OpenAI: canonical flat reasoning_effort without nested 'reasoning' dict
        openai = with_effort_model_params("openai:o3", None, "medium")
        assert openai["reasoning_effort"] == "medium"
        assert "reasoning" not in openai

        # Anthropic: canonical flat reasoning_effort without nested 'thinking' dict
        anthropic = with_effort_model_params("anthropic:claude-sonnet-4", None, "low")
        assert anthropic["reasoning_effort"] == "low"
        assert "thinking" not in anthropic
