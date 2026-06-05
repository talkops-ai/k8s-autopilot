import pytest
from langchain_core.messages import AIMessage
from typing import cast
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerWorkflowState, HelmPlannerState
from k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent import route_after_agent


def test_route_after_agent_ends_when_complete():
    wf = HelmPlannerWorkflowState(
        req_analyser_complete=True,
        architecture_planner_complete=True,
        workflow_complete=True,
    )
    state = cast(HelmPlannerState, {"workflow_state": wf, "messages": []})
    assert route_after_agent(state) == "__end__"


def test_route_after_agent_ends_on_ai_message_no_tools():
    state = cast(HelmPlannerState, {
        "messages": [AIMessage(content="I am done")],
        "active_agent": "requirements_analyser"
    })
    assert route_after_agent(state) == "__end__"


def test_route_after_agent_routes_to_next_agent():
    wf = HelmPlannerWorkflowState(next_agent="architecture_planner")
    state = cast(HelmPlannerState, {"workflow_state": wf, "messages": []})
    assert route_after_agent(state) == "architecture_planner"
