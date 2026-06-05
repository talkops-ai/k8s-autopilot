import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent import (
    HelmPlannerSupervisorAgent,
)


@pytest.fixture
def planner(mock_config):
    return HelmPlannerSupervisorAgent(config=mock_config)


def test_input_transform_extracts_user_query_from_messages(planner):
    payload = {
        "messages": [HumanMessage(content="Create a Helm chart for nginx")],
    }
    result = planner.input_transform(payload)
    assert result["user_query"] == "Create a Helm chart for nginx"


def test_input_transform_prefers_explicit_user_query(planner):
    payload = {
        "messages": [HumanMessage(content="Create a Helm chart for nginx")],
        "user_query": "Explicit query",
    }
    result = planner.input_transform(payload)
    assert result["user_query"] == "Explicit query"


def test_input_transform_resets_workflow_state(planner):
    payload = {
        "messages": [HumanMessage(content="Plan chart")],
        "workflow_state": {
            "req_analyser_complete": True,
            "architecture_planner_complete": True,
        },
    }
    result = planner.input_transform(payload)
    # Must always reset to fresh pipeline start
    assert result["workflow_state"]["req_analyser_complete"] is False
    assert result["workflow_state"]["architecture_planner_complete"] is False
    assert result["active_agent"] == "requirements_analyser"


def test_input_transform_sets_req_analyser_phase(planner):
    payload = {"messages": [HumanMessage(content="Plan chart")]}
    result = planner.input_transform(payload)
    assert result["active_agent"] == "requirements_analyser"
    assert result["current_step"] == "req_analyser"


def test_input_transform_preserves_session_id(planner):
    payload = {"session_id": "session-123"}
    result = planner.input_transform(payload)
    assert result["session_id"] == "session-123"


def test_input_transform_preserves_files(planner):
    payload = {"files": {"test.txt": "content"}}
    result = planner.input_transform(payload)
    assert result["files"] == {"test.txt": "content"}


def test_input_transform_handles_empty_payload(planner):
    payload = {}
    result = planner.input_transform(payload)
    assert result["user_query"] == ""
    assert result["messages"] == []
