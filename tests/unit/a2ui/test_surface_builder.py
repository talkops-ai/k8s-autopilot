"""Unit tests for A2UI surface builder — covers all unified surfaces."""

from __future__ import annotations

import pytest

from k8s_autopilot.a2ui.surface_builder import (
    TALKOPS_CATALOG_ID,
    build_ask_user_surface,
    build_goal_confirmation_surface,
    build_hitl_approval_surface,
    build_plan_todo_surface,
    build_thought_block_surface,
    build_tool_execution_surface,
    build_walkthrough_surface,
    update_ask_user_data,
    update_goal_confirmation_data,
    update_plan_todo_data,
    update_thought_block_data,
    update_tool_execution_data,
    update_walkthrough_data,
)


class TestAskUserSurface:
    def test_build_ask_user_surface_single_question(self):
        questions = [{"question": "What namespace?", "type": "text", "required": True}]
        ops = build_ask_user_surface(
            surface_id="ask-user-123",
            questions=questions,
        )
        assert len(ops) == 3
        # 1. createSurface
        assert ops[0].get("createSurface", {}).get("surfaceId") == "ask-user-123"
        assert ops[0].get("createSurface", {}).get("catalogId") == TALKOPS_CATALOG_ID

        # 2. updateComponents
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert len(comps) == 1
        assert "askUserCard" in comps[0].get("component", {})

        # 3. updateDataModel
        data = ops[2].get("updateDataModel", {}).get("value", {})
        assert data.get("title") == "Agent has a question for you"
        assert data.get("questions") == questions
        assert data.get("onAnswerActionId") == "ask_user_response"
        assert data.get("status") == "pending"

    def test_build_ask_user_surface_multiple_questions_custom_title(self):
        questions = [
            {"question": "Choose cluster", "type": "multiple_choice", "choices": [{"value": "dev"}, {"value": "prod"}]},
            {"question": "Explain reason", "type": "text", "required": False},
        ]
        ops = build_ask_user_surface(
            surface_id="ask-user-456",
            questions=questions,
            title="Cluster Selection",
            action_id="custom_ask_action",
        )
        data = ops[2].get("updateDataModel", {}).get("value", {})
        assert data.get("title") == "Cluster Selection"
        assert len(data.get("questions")) == 2
        assert data.get("onAnswerActionId") == "custom_ask_action"

    def test_update_ask_user_data(self):
        op = update_ask_user_data("ask-user-123", status="answered")
        assert op.get("updateDataModel", {}).get("surfaceId") == "ask-user-123"
        assert op.get("updateDataModel", {}).get("value", {}).get("status") == "answered"


class TestGoalConfirmationSurface:
    def test_build_goal_confirmation_surface(self):
        criteria = ["Pods in default namespace are healthy", "Zero CrashLoopBackOff pods"]
        ops = build_goal_confirmation_surface(
            surface_id="goal-confirm-123",
            goal_text="Diagnose and resolve crashing pods",
            criteria=criteria,
        )
        assert len(ops) == 3
        # createSurface
        assert ops[0].get("createSurface", {}).get("surfaceId") == "goal-confirm-123"

        # updateComponents
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert "goalConfirmationCard" in comps[0].get("component", {})

        # updateDataModel
        data = ops[2].get("updateDataModel", {}).get("value", {})
        assert data.get("goalText") == "Diagnose and resolve crashing pods"
        assert data.get("criteria") == criteria
        assert data.get("status") == "in_progress"
        assert data.get("onDecisionActionId") == "goal_response"

    def test_update_goal_confirmation_data(self):
        op = update_goal_confirmation_data("goal-confirm-123", status="achieved")
        assert op.get("updateDataModel", {}).get("surfaceId") == "goal-confirm-123"
        assert op.get("updateDataModel", {}).get("value", {}).get("status") == "achieved"


class TestHitlApprovalSurface:
    def test_build_hitl_approval_surface(self):
        ops = build_hitl_approval_surface(
            surface_id="hitl-123",
            proposed_action="Apply Kubernetes deployment patch",
            justification="Increases replicas to 3",
            risk_level="high",
            parameters=[{"key": "replicas", "value": "3"}],
        )
        assert len(ops) == 3
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert "hitlApprovalCard" in comps[0].get("component", {})
        data = ops[2].get("updateDataModel", {}).get("value", {})
        assert data.get("proposedAction") == "Apply Kubernetes deployment patch"
        assert data.get("riskLevel") == "high"
        options = data.get("options", [])
        assert len(options) == 3
        assert options[0]["id"] == "approve"
        assert "Approve" in options[0]["label"]
        assert options[1]["id"] == "auto_approve_all"
        assert "Enable Auto" in options[1]["label"]
        assert options[2]["id"] == "reject"
        assert "Reject" in options[2]["label"]


class TestToolExecutionSurface:
    def test_build_tool_execution_surface(self):
        ops = build_tool_execution_surface(
            surface_id="tool-123",
            tool_name="kubectl_get_pods",
            status="running",
            parameters={"namespace": "default"},
        )
        assert len(ops) == 3
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert "toolExecutionCard" in comps[0].get("component", {})
        data = ops[2].get("updateDataModel", {}).get("value", {})
        assert data.get("toolName") == "kubectl_get_pods"
        assert data.get("status") == "running"

    def test_update_tool_execution_data(self):
        op = update_tool_execution_data("tool-123", status="success", duration_ms=250)
        assert op.get("updateDataModel", {}).get("value", {}).get("status") == "success"


class TestThoughtBlockSurface:
    def test_build_thought_block_surface(self):
        ops = build_thought_block_surface(
            surface_id="thought-123",
            title="Analyzing logs",
            summary="Identified OOMKilled container in worker pod",
            severity="warning",
        )
        assert len(ops) == 3
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert "thoughtBlock" in comps[0].get("component", {})
        data = ops[2].get("updateDataModel", {}).get("value", {})
        assert data.get("title") == "Analyzing logs"
        assert data.get("severity") == "warning"

    def test_update_thought_block_data(self):
        op = update_thought_block_data("thought-123", summary="Updated reasoning")
        assert op.get("updateDataModel", {}).get("value", {}).get("summary") == "Updated reasoning"


class TestPlanAndWalkthroughSurfaces:
    def test_build_plan_todo_surface(self):
        todos = [{"id": "1", "task": "Inspect events", "status": "completed"}]
        ops = build_plan_todo_surface(
            surface_id="plan-123",
            todos=todos,
            plan_title="Troubleshooting Plan",
        )
        assert len(ops) == 3
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert "planTodoList" in comps[0].get("component", {})

    def test_build_walkthrough_surface(self):
        ops = build_walkthrough_surface(
            surface_id="walkthrough-123",
            walkthrough="All pods restarted cleanly.",
            status="success",
            total_tasks=2,
            completed_tasks=2,
        )
        assert len(ops) == 3
        comps = ops[1].get("updateComponents", {}).get("components", [])
        assert "executionWalkthrough" in comps[0].get("component", {})
