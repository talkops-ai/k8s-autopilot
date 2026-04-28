from typing import Annotated, Optional, Dict, List, Any, Literal
from typing_extensions import NotRequired
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langchain.agents import AgentState

def _get_current_utc_time() -> datetime:
    return datetime.now(tz=timezone.utc)


class HelmPlannerWorkflowState(BaseModel):
    """Workflow state tracking for the Helm planner 2-phase pipeline.

    Pipeline: RequirementsAnalyser → ArchitecturePlanner
    """
    current_phase: Literal[
        "req_analyser",
        "architecture_planner",
        "complete",
    ] = Field(default="req_analyser", description="Current workflow phase")

    req_analyser_complete: bool = Field(default=False, description="Requirements analysis phase complete")
    architecture_planner_complete: bool = Field(default=False, description="Architecture planning phase complete")

    # Workflow control
    workflow_complete: bool = Field(default=False, description="Overall workflow complete")
    last_agent: Optional[str] = Field(default=None, description="Last agent that completed")
    next_agent: Optional[str] = Field(default=None, description="Next agent to invoke")

    # Workflow metadata
    workflow_id: str = Field(default="", description="Unique workflow identifier")
    started_at: datetime = Field(default_factory=_get_current_utc_time, description="Workflow start time")

    @property
    def is_complete(self) -> bool:
        """Check if all required phases are complete."""
        return all([
            self.req_analyser_complete,
            self.architecture_planner_complete,
        ])

    @property
    def next_phase(self) -> Optional[str]:
        """Determine the next phase based on completion status."""
        if not self.req_analyser_complete:
            return "req_analyser"
        elif not self.architecture_planner_complete:
            return "architecture_planner"
        else:
            return "complete"

    def set_phase_complete(self, phase: str) -> None:
        """Mark a specific phase as complete and update state."""
        if phase == "req_analyser":
            self.req_analyser_complete = True
            self.current_phase = "req_analyser"
        elif phase == "architecture_planner":
            self.architecture_planner_complete = True
            self.current_phase = "architecture_planner"
        else:
            self.workflow_complete = True
            self.current_phase = "complete"

        # Check if workflow is complete
        if self.is_complete:
            self.workflow_complete = True
            self.current_phase = "complete"

    def get_workflow_progress(self) -> Dict[str, Any]:
        """Get current workflow progress for monitoring."""
        return {
            "current_phase": self.current_phase,
            "req_analyser_complete": self.req_analyser_complete,
            "architecture_planner_complete": self.architecture_planner_complete,
            "workflow_complete": self.workflow_complete,
        }


def merge_dicts(x: Optional[Dict[str, Any]], y: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reducer for merging dict states."""
    return {**(x or {}), **(y or {})}

class HelmPlannerState(AgentState):
    """State for the Helm Planner 2-phase pipeline subgraph.

    Compatible with the existing ``PlanningSwarmState`` — both phases read
    from and write to ``handoff_data`` and ``chart_plan`` using the same
    keys the existing parser/analyzer tools expect.

    Reference: k8s_autopilot PlanningSwarmState + aws-orchestrator TFPlannerState
    """
    # ── Message History ──
    messages: Annotated[List[AnyMessage], add_messages]

    # ── Workflow Identification ──
    user_query: NotRequired[str]
    session_id: NotRequired[str]
    task_id: NotRequired[str]

    # ── Handoff State (controls routing between agents) ──
    workflow_state: NotRequired[HelmPlannerWorkflowState]
    status: NotRequired[str]
    active_agent: NotRequired[str]
    current_step: NotRequired[str]

    # ── Phase Outputs (matching PlanningSwarmState keys) ──
    handoff_data: NotRequired[Annotated[Dict[str, Any], merge_dicts]]
    chart_plan: NotRequired[Annotated[Dict[str, Any], merge_dicts]]
    updated_user_requirements: NotRequired[str]
    question_asked: NotRequired[str]

    # ── Error Tracking ──
    error_state: NotRequired[Any]  # ErrorContext

    # ── Virtual Filesystem ──
    files: NotRequired[Dict[str, Any]]

    # ── HITL ──
    pending_feedback_requests: NotRequired[Dict[str, Any]]