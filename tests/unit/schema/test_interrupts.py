"""Unit tests for typed interrupt schemas."""

from __future__ import annotations

import json
from k8s_autopilot.schema.interrupts import (
    AskUserResumePayload,
    GoalReviewResumePayload,
    HitlDecision,
    HitlResumePayload,
)


class TestHitlResumePayload:
    def test_approve_string(self):
        payload = HitlResumePayload.from_raw("approve")
        assert len(payload.decisions) == 1
        assert payload.decisions[0].type == "approve"
        assert not payload.auto_approve_requested

    def test_auto_approve_string(self):
        payload = HitlResumePayload.from_raw("auto_approve_all")
        assert len(payload.decisions) == 1
        assert payload.decisions[0].type == "approve"
        assert payload.auto_approve_requested

    def test_reject_string(self):
        payload = HitlResumePayload.from_raw("reject")
        assert len(payload.decisions) == 1
        assert payload.decisions[0].type == "reject"

    def test_json_string_decisions(self):
        raw = json.dumps({"decisions": [{"type": "approve"}]})
        payload = HitlResumePayload.from_raw(raw)
        assert len(payload.decisions) == 1
        assert payload.decisions[0].type == "approve"

    def test_hitl_response_dict(self):
        raw = {"action": "hitl_response", "decision": "approve"}
        payload = HitlResumePayload.from_raw(raw)
        assert payload.decisions[0].type == "approve"

    def test_hitl_rejection_with_message(self):
        raw = {"action": "hitl_response", "decision": "reject", "rejectionReason": "destructive"}
        payload = HitlResumePayload.from_raw(raw)
        assert payload.decisions[0].type == "reject"
        assert payload.decisions[0].message == "destructive"


class TestAskUserResumePayload:
    def test_simple_string_answer(self):
        payload = AskUserResumePayload.from_raw("test answer")
        assert payload.status == "answered"
        assert payload.answers == ["test answer"]

    def test_cancelled_string(self):
        payload = AskUserResumePayload.from_raw("cancel", questions=["q1", "q2"])
        assert payload.status == "cancelled"
        assert payload.answers == ["(cancelled)", "(cancelled)"]

    def test_dict_choice(self):
        payload = AskUserResumePayload.from_raw({"choice": "Option A"})
        assert payload.status == "answered"
        assert payload.answers == ["Option A"]

    def test_dict_answers_list(self):
        payload = AskUserResumePayload.from_raw({"status": "answered", "answers": ["Ans 1", "Ans 2"]})
        assert payload.status == "answered"
        assert payload.answers == ["Ans 1", "Ans 2"]


class TestGoalReviewResumePayload:
    def test_confirm_string(self):
        payload = GoalReviewResumePayload.from_raw("confirm")
        assert payload.decision == "confirm"

    def test_approve_alias(self):
        payload = GoalReviewResumePayload.from_raw("approve")
        assert payload.decision == "confirm"

    def test_ui_goal_response(self):
        raw = {"action": "goal_response", "decision": "confirm"}
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "confirm"

    def test_ui_hitl_approval_shape(self):
        raw = {"action": "hitl_response", "decision": "approve"}
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "confirm"

    def test_edit_decision_with_criteria_list(self):
        raw = {
            "decision": "edit",
            "criteria": ["Pod is running", "Service responds 200"],
        }
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "edit"
        assert payload.criteria == ["Pod is running", "Service responds 200"]

    def test_edit_decision_with_criteria_text(self):
        raw = {
            "decision": "edit",
            "criteria": "- First step\n- Second step",
        }
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "edit"
        assert payload.criteria == ["First step", "Second step"]

    def test_reject_with_feedback(self):
        raw = {
            "decision": "reject",
            "feedback": "Use Helm instead of raw manifests",
        }
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "reject"
        assert payload.feedback == "Use Helm instead of raw manifests"

    def test_cancel_decision(self):
        payload = GoalReviewResumePayload.from_raw("dismiss")
        assert payload.decision == "cancel"

    def test_nested_interrupt_id_dict(self):
        raw = {"int_12345": {"decision": "confirm"}}
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "confirm"
