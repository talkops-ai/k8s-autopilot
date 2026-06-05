import pytest
from typing import cast
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerWorkflowState, HelmPlannerState
from k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent import route_initial


def test_route_initial_uses_workflow_state_next_agent():
    wf = HelmPlannerWorkflowState(next_agent="architecture_planner")
    state = cast(HelmPlannerState, {"workflow_state": wf, "messages": []})
    assert route_initial(state) == "architecture_planner"


def test_route_initial_falls_back_to_active_agent():
    state = cast(HelmPlannerState, {"active_agent": "requirements_analyser", "messages": []})
    assert route_initial(state) == "requirements_analyser"
