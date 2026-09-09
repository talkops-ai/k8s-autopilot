"""Unit tests for GoalCriteriaMiddleware and nested criteria agents."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from k8s_autopilot.middleware.goal_criteria import (
    GoalAmendRequest,
    GoalCreateRequest,
    GoalCriteriaMiddleware,
    GoalCriteriaState,
    _coerce_goal_proposal,
    _goal_criteria_request,
)


class TestGoalCriteriaValidation:
    def test_valid_create_request(self) -> None:
        raw = {"request_id": "req-1", "kind": "create", "objective": "Scale nginx to 3 replicas"}
        parsed = _goal_criteria_request(raw)
        assert parsed["kind"] == "create"
        assert parsed["objective"] == "Scale nginx to 3 replicas"
        assert parsed["request_id"] == "req-1"

    def test_valid_amend_request(self) -> None:
        raw = {
            "request_id": "req-2",
            "kind": "amend",
            "objective": "Scale nginx",
            "criteria": "Old criteria",
            "feedback": "Add healthcheck criteria",
        }
        parsed = _goal_criteria_request(raw)
        assert parsed["kind"] == "amend"
        assert parsed["criteria"] == "Old criteria"
        assert parsed["feedback"] == "Add healthcheck criteria"

    def test_invalid_request_type(self) -> None:
        with pytest.raises(TypeError, match="must be an object"):
            _goal_criteria_request("invalid")

    def test_missing_request_id(self) -> None:
        with pytest.raises(ValueError, match="requires a request_id"):
            _goal_criteria_request({"kind": "create", "objective": "test"})

    def test_missing_objective(self) -> None:
        with pytest.raises(ValueError, match="requires an objective"):
            _goal_criteria_request({"request_id": "req-1", "kind": "create"})

    def test_coerce_goal_proposal(self) -> None:
        data = {"objective": "Deploy Redis", "criteria": "Redis pod running and ready"}
        assert _coerce_goal_proposal(data) == ("Deploy Redis", "Redis pod running and ready")

        nested = {"structured_response": {"objective": "Deploy Redis", "criteria": "Redis pod ready"}}
        assert _coerce_goal_proposal(nested) == ("Deploy Redis", "Redis pod ready")


class TestGoalCriteriaMiddleware:
    def test_no_request_passthrough(self) -> None:
        mw = GoalCriteriaMiddleware()
        runtime = MagicMock()
        state: GoalCriteriaState = {}
        assert mw.before_agent(state, runtime) is None

    def test_before_agent_executes_criteria_agent(self) -> None:
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "objective": "Target Objective",
            "criteria": "- Criteria 1\n- Criteria 2",
        }
        mw = GoalCriteriaMiddleware(criteria_agent=mock_agent)
        runtime = MagicMock()
        runtime.context = {}

        state: GoalCriteriaState = {
            "goal_criteria_request": {
                "request_id": "req-100",
                "kind": "create",
                "objective": "Scale deployment",
            },
            "messages": [],
        }

        update = mw.before_agent(state, runtime)
        assert update is not None
        assert update.get("jump_to") == "end"
        assert update.get("goal_criteria_request") is None
        assert update.get("_pending_goal_objective") == "Scale deployment"
        assert update.get("_pending_goal_rubric") == "- Criteria 1\n- Criteria 2"
        assert update.get("_pending_goal_request_id") == "req-100"
