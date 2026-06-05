import pytest
from langchain_core.messages import AIMessage
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerState, HelmPlannerWorkflowState
from k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent import (
    HelmPlannerSupervisorAgent,
)


@pytest.fixture
def planner(mock_config):
    return HelmPlannerSupervisorAgent(config=mock_config)


def test_output_transform_returns_ai_message(planner):
    wf = HelmPlannerWorkflowState(workflow_complete=True)
    state = HelmPlannerState(
        messages=[],
        user_query="",
        workflow_state=wf.model_dump(),
        active_agent="req_analyser",
        current_step="req_analyser",
        status="completed",
        task_id="test",
        session_id="test",
        files={},
        handoff_data={},
        chart_plan={},
        question_asked="",
        updated_user_requirements=""
    )
    result = planner.output_transform(state)
    assert isinstance(result["messages"][-1], AIMessage)


def test_output_transform_includes_app_name_in_summary(planner):
    state = {
        "handoff_data": {
            "parsed_requirements": {"application_name": "nginx"}
        }
    }
    result = planner.output_transform(state)
    assert "nginx" in result["messages"][-1].content


def test_output_transform_lists_skill_files(planner):
    state = {
        "files": {"/skills/nginx-chart-generator/SKILL.md": "content"}
    }
    result = planner.output_transform(state)
    assert "Skills written" in result["messages"][-1].content
    assert "/skills/nginx-chart-generator/SKILL.md" in result["messages"][-1].content


def test_output_transform_merges_parent_files(planner):
    state = {
        "files": {"/skills/nginx-chart-generator/SKILL.md": "content"}
    }
    parent_files = {"/skills/common.md": "content"}
    result = planner.output_transform(state, parent_files=parent_files)
    assert "/skills/nginx-chart-generator/SKILL.md" in result["files"]
    assert "/skills/common.md" in result["files"]


def test_output_transform_handles_empty_state(planner):
    state = {}
    result = planner.output_transform(state)
    assert isinstance(result["messages"][-1], AIMessage)
    assert isinstance(result["files"], dict)


def test_output_transform_includes_chart_name(planner):
    state = {
        "chart_plan": {"chart_name": "nginx"}
    }
    result = planner.output_transform(state)
    assert "nginx" in result["messages"][-1].content
