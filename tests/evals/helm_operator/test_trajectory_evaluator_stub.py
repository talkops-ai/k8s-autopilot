import pytest


@pytest.mark.eval
def test_trajectory_evaluator_contract():
    trace = {"tool_calls": [{"tool_name": "helm_template"}], "actions": [{"action": "deploy"}], "final_outcome": "deployed"}
    scenario = {"expectations": {"must_call_tools": ["helm_template"], "final_outcome": "deployed"}}
    assert trace["final_outcome"] == scenario["expectations"]["final_outcome"]
