import pytest
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerWorkflowState


def test_is_complete_requires_both_phases():
    wf = HelmPlannerWorkflowState(req_analyser_complete=True)
    assert wf.is_complete is False


def test_is_complete_true_when_both_done():
    wf = HelmPlannerWorkflowState(
        req_analyser_complete=True,
        architecture_planner_complete=True,
    )
    assert wf.is_complete is True


def test_next_phase_returns_req_analyser_first():
    wf = HelmPlannerWorkflowState()
    assert wf.next_phase == "req_analyser"


def test_next_phase_returns_arch_planner_after_req():
    wf = HelmPlannerWorkflowState(req_analyser_complete=True)
    assert wf.next_phase == "architecture_planner"


def test_next_phase_returns_complete_when_done():
    wf = HelmPlannerWorkflowState(
        req_analyser_complete=True,
        architecture_planner_complete=True,
    )
    assert wf.next_phase == "complete"


def test_set_phase_complete_req_analyser():
    wf = HelmPlannerWorkflowState()
    wf.set_phase_complete("req_analyser")
    assert wf.req_analyser_complete is True
    assert wf.current_phase == "req_analyser"


def test_set_phase_complete_triggers_workflow_complete():
    wf = HelmPlannerWorkflowState()
    wf.set_phase_complete("req_analyser")
    wf.set_phase_complete("architecture_planner")
    assert wf.workflow_complete is True
    assert wf.current_phase == "complete"


def test_get_workflow_progress_structure():
    wf = HelmPlannerWorkflowState()
    progress = wf.get_workflow_progress()
    assert "current_phase" in progress
    assert "req_analyser_complete" in progress
    assert "architecture_planner_complete" in progress
    assert "workflow_complete" in progress
