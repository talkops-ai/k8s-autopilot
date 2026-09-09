"""Unit tests for GoalRubricChannels and goal state helpers."""

from __future__ import annotations

from k8s_autopilot.middleware.resume_state import ResumeState
from k8s_autopilot.state.goal_channels import (
    GoalProposalKind,
    GoalRubricChannels,
    GoalStatus,
    coerce_goal_proposal_kind,
    coerce_goal_status,
)
from k8s_autopilot.tools.goal_tools import GoalToolState


class TestGoalChannels:
    def test_coerce_goal_status(self):
        assert coerce_goal_status("active") == "active"
        assert coerce_goal_status("blocked") == "blocked"
        assert coerce_goal_status("paused") == "paused"
        assert coerce_goal_status("complete") == "complete"
        assert coerce_goal_status("unknown") is None
        assert coerce_goal_status(None) is None
        assert coerce_goal_status(123) is None

    def test_coerce_goal_proposal_kind(self):
        assert coerce_goal_proposal_kind("create") == "create"
        assert coerce_goal_proposal_kind("amend") == "amend"
        assert coerce_goal_proposal_kind("delete") is None
        assert coerce_goal_proposal_kind(None) is None

    def test_resume_state_inherits_goal_rubric_channels(self):
        assert GoalRubricChannels in ResumeState.__orig_bases__
        assert "_goal_objective" in ResumeState.__annotations__
        assert "_goal_status" in ResumeState.__annotations__
        assert "_goal_rubric" in ResumeState.__annotations__
        assert "_context_tokens" in ResumeState.__annotations__

    def test_goal_tool_state_inherits_goal_rubric_channels(self):
        assert GoalRubricChannels in GoalToolState.__orig_bases__
        assert "_goal_objective" in GoalToolState.__annotations__
        assert "_goal_status" in GoalToolState.__annotations__
        assert "rubric" in GoalToolState.__annotations__
