"""Unit tests for goal tools, goal state snapshots, and goal-state notices.

Tests the GoalToolsMiddleware, goal tool snapshots (get_goal, get_rubric,
update_goal), goal state notice generation, fingerprinting, continuations,
and notice lifecycle.
"""

from __future__ import annotations

from typing import Any

import pytest


# ============================================================================
# Goal Tool Snapshots
# ============================================================================


class TestGoalToolNames:
    def test_exported_names(self):
        from k8s_autopilot.tools.goal_tools import GOAL_TOOL_NAMES

        assert GOAL_TOOL_NAMES == frozenset({"get_goal", "get_rubric", "update_goal", "propose_goal"})


class TestCleanStateText:
    def test_strips_whitespace(self):
        from k8s_autopilot.tools.goal_tools import _clean_state_text

        assert _clean_state_text({"k": "  value  "}, "k") == "value"

    def test_returns_none_for_empty(self):
        from k8s_autopilot.tools.goal_tools import _clean_state_text

        assert _clean_state_text({"k": "   "}, "k") is None
        assert _clean_state_text({"k": ""}, "k") is None

    def test_returns_none_for_missing(self):
        from k8s_autopilot.tools.goal_tools import _clean_state_text

        assert _clean_state_text({}, "k") is None

    def test_returns_none_for_non_string(self):
        from k8s_autopilot.tools.goal_tools import _clean_state_text

        assert _clean_state_text({"k": 42}, "k") is None


class TestRubricSnapshot:
    def test_empty_state(self):
        from k8s_autopilot.tools.goal_tools import _rubric_snapshot

        snap = _rubric_snapshot({})
        assert snap["active"] is False
        assert snap["criteria"] is None
        assert snap["grading_status"] is None

    def test_invocation_rubric_takes_precedence(self):
        from k8s_autopilot.tools.goal_tools import _rubric_snapshot

        snap = _rubric_snapshot({"rubric": "Must pass all tests."})
        assert snap["active"] is True
        assert snap["criteria"] == "Must pass all tests."

    def test_goal_rubric_when_goal_active(self):
        from k8s_autopilot.tools.goal_tools import _rubric_snapshot

        snap = _rubric_snapshot({
            "_goal_objective": "Finish feature X",
            "_goal_status": "active",
            "_goal_rubric": "Feature X criteria",
        })
        assert snap["active"] is True
        assert snap["criteria"] == "Feature X criteria"

    def test_standalone_sticky_rubric(self):
        from k8s_autopilot.tools.goal_tools import _rubric_snapshot

        snap = _rubric_snapshot({"_sticky_rubric": "Standalone rubric"})
        assert snap["active"] is True
        assert snap["criteria"] == "Standalone rubric"

    def test_sticky_matching_goal_rubric_suppressed(self):
        from k8s_autopilot.tools.goal_tools import _rubric_snapshot

        snap = _rubric_snapshot({
            "_goal_objective": "Finish X",
            "_goal_status": "paused",
            "_goal_rubric": "Same criteria",
            "_sticky_rubric": "Same criteria",
        })
        assert snap["active"] is False
        assert snap["criteria"] is None


class TestGoalSnapshot:
    def test_no_goal(self):
        from k8s_autopilot.tools.goal_tools import _goal_snapshot

        snap = _goal_snapshot({})
        assert snap["active"] is False
        assert snap["objective"] is None
        assert snap["status"] is None
        assert snap["note"] is None

    def test_active_goal(self):
        from k8s_autopilot.tools.goal_tools import _goal_snapshot

        snap = _goal_snapshot({
            "_goal_objective": "Deploy microservice",
            "_goal_status": "active",
            "_goal_rubric": "Must be zero-downtime",
            "_goal_status_note": "Working on it",
        })
        assert snap["active"] is True
        assert snap["objective"] == "Deploy microservice"
        assert snap["status"] == "active"
        assert snap["criteria"] == "Must be zero-downtime"
        assert snap["note"] == "Working on it"

    def test_blocked_is_actionable(self):
        from k8s_autopilot.tools.goal_tools import _goal_snapshot

        snap = _goal_snapshot({
            "_goal_objective": "Deploy microservice",
            "_goal_status": "blocked",
            "_goal_status_note": "Need more info",
        })
        assert snap["active"] is True
        assert snap["status"] == "blocked"

    def test_paused_not_actionable(self):
        from k8s_autopilot.tools.goal_tools import _goal_snapshot

        snap = _goal_snapshot({
            "_goal_objective": "Deploy microservice",
            "_goal_status": "paused",
        })
        assert snap["active"] is False

    def test_complete_not_actionable(self):
        from k8s_autopilot.tools.goal_tools import _goal_snapshot

        snap = _goal_snapshot({
            "_goal_objective": "Deploy microservice",
            "_goal_status": "complete",
        })
        assert snap["active"] is False

    def test_unknown_status_coerced_to_active(self):
        from k8s_autopilot.tools.goal_tools import _goal_snapshot

        snap = _goal_snapshot({
            "_goal_objective": "Deploy microservice",
            "_goal_status": "banana",
        })
        assert snap["active"] is True
        assert snap["status"] == "active"


class TestUpdateGoalCommand:
    def test_no_active_goal(self):
        from k8s_autopilot.tools.goal_tools import _update_goal_command

        result = _update_goal_command(
            status="complete", note="Done", tool_call_id="tc-1", state={},
        )
        assert "No active goal" in result.update["messages"][0].content

    def test_paused_goal_rejected(self):
        from k8s_autopilot.tools.goal_tools import _update_goal_command

        result = _update_goal_command(
            status="blocked", note="Stuck", tool_call_id="tc-1",
            state={"_goal_objective": "Test", "_goal_status": "paused"},
        )
        assert "paused" in result.update["messages"][0].content.lower()

    def test_complete_stages_pending_note(self):
        from k8s_autopilot.tools.goal_tools import _update_goal_command

        result = _update_goal_command(
            status="complete", note="All tests pass", tool_call_id="tc-1",
            state={"_goal_objective": "Test", "_goal_status": "active"},
        )
        assert result.update["_pending_goal_completion_note"] == "All tests pass"

    def test_blocked_commits_immediately(self):
        from k8s_autopilot.tools.goal_tools import _update_goal_command

        result = _update_goal_command(
            status="blocked", note="Need API key", tool_call_id="tc-1",
            state={"_goal_objective": "Test", "_goal_status": "active"},
        )
        assert result.update["_goal_status"] == "blocked"
        assert result.update["_goal_status_note"] == "Need API key"

    def test_empty_note_rejected(self):
        from k8s_autopilot.tools.goal_tools import _update_goal_command

        result = _update_goal_command(
            status="complete", note="   ", tool_call_id="tc-1",
            state={"_goal_objective": "Test", "_goal_status": "active"},
        )
        assert "evidence" in result.update["messages"][0].content.lower()


# ============================================================================
# Goal State Notice
# ============================================================================


class TestGoalStateProjection:
    def test_empty_state(self):
        from k8s_autopilot.middleware.goal_state_notice import project_goal_state

        p = project_goal_state({})
        assert p["goal_objective"] is None
        assert p["goal_actionable"] is False

    def test_active_goal_with_rubric(self):
        from k8s_autopilot.middleware.goal_state_notice import project_goal_state

        p = project_goal_state({
            "_goal_objective": "Deploy feature",
            "_goal_status": "active",
            "_goal_rubric": "Must pass CI",
        })
        assert p["goal_actionable"] is True
        assert p["rubric_criteria"] == "Must pass CI"
        assert p["rubric_source"] == "goal"

    def test_invocation_rubric_precedence(self):
        from k8s_autopilot.middleware.goal_state_notice import project_goal_state

        p = project_goal_state({
            "rubric": "Invocation rubric",
            "_sticky_rubric": "Sticky rubric",
        })
        assert p["rubric_criteria"] == "Invocation rubric"
        assert p["rubric_source"] == "invocation"


class TestGoalStateFingerprint:
    def test_deterministic(self):
        from k8s_autopilot.middleware.goal_state_notice import goal_state_fingerprint

        state = {"_goal_objective": "Test", "_goal_status": "active"}
        assert goal_state_fingerprint(state) == goal_state_fingerprint(state)

    def test_changes_with_state(self):
        from k8s_autopilot.middleware.goal_state_notice import goal_state_fingerprint

        fp1 = goal_state_fingerprint({"_goal_objective": "A"})
        fp2 = goal_state_fingerprint({"_goal_objective": "B"})
        assert fp1 != fp2


class TestHasGoalOrRubricState:
    def test_empty(self):
        from k8s_autopilot.middleware.goal_state_notice import has_goal_or_rubric_state

        assert not has_goal_or_rubric_state({})

    def test_with_goal(self):
        from k8s_autopilot.middleware.goal_state_notice import has_goal_or_rubric_state

        assert has_goal_or_rubric_state({"_goal_objective": "Test"})

    def test_with_rubric(self):
        from k8s_autopilot.middleware.goal_state_notice import has_goal_or_rubric_state

        assert has_goal_or_rubric_state({"_sticky_rubric": "Test rubric"})


class TestBuildGoalStateNotice:
    def test_active_goal(self):
        from k8s_autopilot.middleware.goal_state_notice import build_goal_state_notice

        notice = build_goal_state_notice(
            {"_goal_objective": "Deploy", "_goal_status": "active"},
            event_id="ev-1",
        )
        assert "Goal/rubric state changed" in notice.content
        assert "actionable: yes" in notice.content
        assert notice.id == "ev-1"

    def test_notice_info_extraction(self):
        from k8s_autopilot.middleware.goal_state_notice import (
            build_goal_state_notice, goal_state_notice_info,
        )

        notice = build_goal_state_notice(
            {"_goal_objective": "X"}, event_id="ev-2",
        )
        info = goal_state_notice_info(notice)
        assert info is not None
        assert info["event_id"] == "ev-2"
        assert len(info["state_fingerprint"]) == 64


class TestBuildGoalContinuation:
    def test_created(self):
        from k8s_autopilot.middleware.goal_state_notice import build_goal_continuation

        msg = build_goal_continuation("created", event_id="cont-1")
        assert "Goal set by the user" in msg.content

    def test_unsaved_fallback(self):
        from k8s_autopilot.middleware.goal_state_notice import build_goal_continuation

        msg = build_goal_continuation(
            "created", unsaved_objective="Fix the bug", event_id="cont-2",
        )
        assert "checkpoint write failed" in msg.content
        assert "Fix the bug" in msg.content

    def test_resumed(self):
        from k8s_autopilot.middleware.goal_state_notice import build_goal_continuation

        msg = build_goal_continuation("resumed", event_id="cont-3")
        assert "resumed" in msg.content


class TestLatestGoalStateNotice:
    def test_finds_latest(self):
        from k8s_autopilot.middleware.goal_state_notice import (
            build_goal_state_notice, latest_goal_state_notice,
        )
        from langchain_core.messages import HumanMessage

        notice = build_goal_state_notice({"_goal_objective": "X"}, event_id="ev-3")
        messages = [HumanMessage(content="hi"), notice, HumanMessage(content="bye")]
        result = latest_goal_state_notice(messages)
        assert result is not None
        assert result[0] == 1
        assert result[1]["event_id"] == "ev-3"


# ============================================================================
# GoalToolsMiddleware
# ============================================================================


class TestGoalToolsMiddleware:
    def test_instantiation(self):
        from k8s_autopilot.middleware.goal_tools import GoalToolsMiddleware

        mw = GoalToolsMiddleware()
        assert len(mw.tools) == 4
        assert {t.name for t in mw.tools} == {"get_goal", "get_rubric", "update_goal", "propose_goal"}

    def test_notice_update_no_goal(self):
        from k8s_autopilot.middleware.goal_tools import GoalToolsMiddleware

        assert GoalToolsMiddleware()._notice_update({"messages": []}) is None

    def test_notice_update_with_goal(self):
        from k8s_autopilot.middleware.goal_tools import GoalToolsMiddleware

        result = GoalToolsMiddleware()._notice_update({
            "messages": [],
            "_goal_objective": "Test goal",
            "_goal_status": "active",
        })
        assert result is not None
        assert "Goal/rubric state changed" in result["messages"][0].content


class TestProposeGoalParser:
    def test_confirm_decision(self):
        from k8s_autopilot.tools.goal_tools import _parse_goal_response

        cmd = _parse_goal_response(
            response={"decision": "confirm"},
            objective="Deploy Prometheus",
            criteria=["helm repo add prometheus-community", "helm install prometheus"],
            tool_call_id="call_123",
        )
        assert cmd.update["_goal_objective"] == "Deploy Prometheus"
        assert cmd.update["_goal_status"] == "active"
        assert "- helm install prometheus" in cmd.update["_goal_rubric"]
        assert cmd.update["rubric"] == cmd.update["_goal_rubric"]
        assert len(cmd.update["messages"]) == 1
        assert "Goal confirmed by user" in cmd.update["messages"][0].content

    def test_edit_decision(self):
        from k8s_autopilot.tools.goal_tools import _parse_goal_response

        cmd = _parse_goal_response(
            response={
                "decision": "edit",
                "criteria": ["kubectl apply -f custom.yaml", "verify pods running"],
            },
            objective="Deploy Custom App",
            criteria=["old criteria"],
            tool_call_id="call_456",
        )
        assert cmd.update["_goal_objective"] == "Deploy Custom App"
        assert cmd.update["_goal_status"] == "active"
        assert "- kubectl apply -f custom.yaml" in cmd.update["_goal_rubric"]
        assert "- verify pods running" in cmd.update["_goal_rubric"]
        assert "old criteria" not in cmd.update["_goal_rubric"]
        assert "Goal confirmed with user-edited criteria" in cmd.update["messages"][0].content

    def test_reject_decision(self):
        from unittest.mock import patch
        from k8s_autopilot.tools.goal_tools import _parse_goal_response

        with patch("k8s_autopilot.rubrics.generator.generate_rubric", return_value="- Install Prometheus\n- Configure scraping"):
            cmd = _parse_goal_response(
                response={
                    "decision": "reject",
                    "feedback": "Skip Loki, focus on Prometheus only",
                },
                objective="Setup Observability",
                criteria=["Install Prometheus", "Install Loki"],
                tool_call_id="call_789",
            )
            assert "_goal_objective" not in cmd.update
            assert len(cmd.update["messages"]) == 1
            assert "User rejected proposed goal criteria with feedback" in cmd.update["messages"][0].content
            assert "Skip Loki, focus on Prometheus only" in cmd.update["messages"][0].content
            assert "Regenerated Criteria based on feedback" in cmd.update["messages"][0].content
            assert "- Configure scraping" in cmd.update["messages"][0].content

    def test_cancel_decision(self):
        from k8s_autopilot.tools.goal_tools import _parse_goal_response

        cmd = _parse_goal_response(
            response={"decision": "cancel"},
            objective="Setup Observability",
            criteria=["Install Prometheus"],
            tool_call_id="call_999",
        )
        assert "_goal_objective" not in cmd.update
        assert len(cmd.update["messages"]) == 1
        assert "User dismissed the goal proposal" in cmd.update["messages"][0].content

    def test_propose_goal_auto_generates_criteria(self):
        from unittest.mock import patch
        from k8s_autopilot.tools.goal_tools import propose_goal

        captured_interrupt = None
        def mock_interrupt(req):
            nonlocal captured_interrupt
            captured_interrupt = req
            return {"decision": "confirm"}

        with patch("k8s_autopilot.tools.goal_tools.generate_rubric", return_value="- Deploy Prometheus\n- Configure alert rules"), \
             patch("k8s_autopilot.tools.goal_tools.interrupt", side_effect=mock_interrupt):
            cmd = propose_goal.invoke({
                "args": {"objective": "Deploy Prometheus and Grafana monitoring stack"},
                "name": "propose_goal",
                "type": "tool_call",
                "id": "call_auto_1",
            })
            assert captured_interrupt is not None
            assert captured_interrupt["type"] == "goal_review"
            assert captured_interrupt["objective"] == "Deploy Prometheus and Grafana monitoring stack"
            assert "Deploy Prometheus" in captured_interrupt["criteria"]
            assert "Configure alert rules" in captured_interrupt["criteria"]
            assert cmd.update["_goal_status"] == "active"
            assert "- Deploy Prometheus" in cmd.update["_goal_rubric"]


