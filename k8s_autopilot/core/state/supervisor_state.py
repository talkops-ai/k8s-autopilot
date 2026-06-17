from typing import Annotated, Optional, Dict, List, Any
from typing_extensions import NotRequired
from enum import Enum
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing import TypedDict

class SupervisorWorkflowState(BaseModel):
    """Workflow state tracking for the supervisor.
    
    Tracks which coordinators have completed. Internal workflow details
    (phases, plans, artifacts) stay inside each coordinator's own state.
    
    Reference: aws-orchestrator SupervisorWorkflowState pattern.
    """
    
    # Current workflow phase (set by supervisor router)
    current_phase: Optional[str] = Field(
        default=None,
        description="Current workflow phase or coordinator name",
    )
    
    # Agent handoff tracking
    last_agent: Optional[str] = Field(default=None, description="Last coordinator that ran")
    next_agent: Optional[str] = Field(default=None, description="Next coordinator to invoke")
    
    # Coordinator completion flags (set by coordinator.output_transform())
    helm_operator_complete: bool = Field(default=False)
    app_operator_complete: bool = Field(default=False)
    k8s_operator_complete: bool = Field(default=False)
    
    # Workflow control
    workflow_complete: bool = Field(default=False, description="Overall workflow complete")
    loop_counter: int = Field(default=0, ge=0, le=50, description="Loop counter for infinite loop prevention")
    error_occurred: bool = Field(default=False)
    error_message: Optional[str] = Field(default=None)
    
    def set_phase_complete(self, phase: str) -> None:
        """Mark a specific phase as complete."""
        mapping = {
            "helm_operator": "helm_operator_complete",
            "app_operator": "app_operator_complete",
            "k8s_operator": "k8s_operator_complete",
        }
        attr = mapping.get(phase)
        if attr:
            setattr(self, attr, True)
            self.last_agent = phase
        
        # Check if workflow is complete
        if self._is_complete():
            self.workflow_complete = True
            self.current_phase = "complete"
    
    def _is_complete(self) -> bool:
        """Check if all required phases are complete."""
        return all([
            self.helm_operator_complete,
            self.app_operator_complete,
            self.k8s_operator_complete,
        ])
    
    def get_workflow_progress(self) -> Dict[str, Any]:
        """Get current workflow progress for monitoring."""
        return {
            "current_phase": self.current_phase,
            "last_agent": self.last_agent,
            "next_agent": self.next_agent,
            "helm_operator_complete": self.helm_operator_complete,
            "app_operator_complete": self.app_operator_complete,
            "k8s_operator_complete": self.k8s_operator_complete,
            "workflow_complete": self.workflow_complete,
            "loop_counter": self.loop_counter,
            "error_occurred": self.error_occurred,
        }


class WorkflowStatus(str, Enum):
    """Workflow status enumeration."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_APPROVAL = "human_approval"
    INPUT_REQUIRED = "input_required"
    INTERRUPTED = "interrupted"

class MainSupervisorState(TypedDict, total=False):
    """Minimal supervisor state — coordinators manage their own internal state.
    
    The supervisor is now a router (create_agent + tool wrappers). Each coordinator
    (HelmGeneration, HelmMgmt, ArgoCD) handles its own domain state internally.
    The supervisor only sees messages, workflow progress, and coordinator output summaries.
    
    Reference: aws-orchestrator SupervisorState (~10 fields).
    
    Removed fields (now coordinator-internal):
        llm_input_messages, remaining_steps, user_requirements, active_phase,
        planner_output, helm_chart_artifacts, validation_results,
        helm_mgmt_response, argocd_onboarding_response, human_approval_status,
        tool_call_results_for_review, pending_tool_calls, tool_call_approvals,
        error_state, file_artifacts, todos, workspace_dir
    """
    # ── Required at invocation ─────────────────────────────────────────
    messages: Annotated[List[AnyMessage], add_messages]
    
    # ── Core identifiers ──────────────────────────────────────────────
    user_query: str
    session_id: str
    task_id: str
    
    # ── Runtime context (injected into deep-agent config) ─────────────
    context: Dict[str, Any]
    
    # ── Workflow tracking ─────────────────────────────────────────────
    status: str  # "pending" | "working" | "completed" | "error"
    current_phase: str
    workflow_state: SupervisorWorkflowState
    workflow_complete: bool
    
    # ── Deep agent coordinator outputs (one per coordinator) ──────────
    helm_operator_output: Dict[str, Any]
    app_operator_output: Dict[str, Any]
    k8s_operator_output: Dict[str, Any]
    
    # ── Routing & Handoff tracking ────────────────────────────────────
    active_agent: str
    dialog_state: str
    return_to: str
    correlation_id: str
    handoff_request: Dict[str, Any]
    handoff_result: Any
    
    # ── HITL ──────────────────────────────────────────────────────────
    pending_feedback_requests: Dict[str, Any]
