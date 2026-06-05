from k8s_autopilot.core.state.helm_planner_state import HelmPlannerState, HelmPlannerWorkflowState


def create_planner_state(
    user_query: str = "",
    messages=None,
    req_analyser_complete: bool = False,
    architecture_planner_complete: bool = False,
) -> HelmPlannerState:
    wf = HelmPlannerWorkflowState(
        req_analyser_complete=req_analyser_complete,
        architecture_planner_complete=architecture_planner_complete,
    )
    if req_analyser_complete and architecture_planner_complete:
        wf.current_phase = "complete"
    elif req_analyser_complete:
        wf.current_phase = "architecture_planner"
    else:
        wf.current_phase = "req_analyser"
    
    from typing import cast
    return cast(HelmPlannerState, {
        "messages": messages or [],
        "user_query": user_query,
        "workflow_state": wf.model_dump(),
        "active_agent": "requirements_analyser",
        "current_step": "req_analyser",
        "status": "in_progress",
        "task_id": "test-task",
        "session_id": "test-session",
        "files": {},
    })
