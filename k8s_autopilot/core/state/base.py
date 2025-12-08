from typing import Annotated, TypedDict, Optional, Literal, Dict, List, Any
import operator 
from typing_extensions import NotRequired
from operator import add
from enum import Enum

from datetime import datetime, timezone
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, AnyMessage
from langgraph.graph.message import add_messages
from langchain.agents import AgentState

# ============================================================================
# Shared Data Models
# ============================================================================

class ChartRequirements(BaseModel):
    """User requirements for Helm chart generation"""
    application_name: str = Field(..., description="Name of the application")
    application_type: Literal["web", "api", "worker", "database", "cache"] = "web"
    container_image: str = Field(..., description="Container image to deploy")
    replicas: int = Field(default=3, ge=1, le=100)
    resource_requests: Dict[str, str] = Field(
        default={"cpu": "100m", "memory": "128Mi"}
    )
    resource_limits: Dict[str, str] = Field(
        default={"cpu": "500m", "memory": "512Mi"}
    )
    environment_variables: Dict[str, str] = Field(default_factory=dict)
    config_maps: List[str] = Field(default_factory=list)
    secrets: List[str] = Field(default_factory=list)
    ingress_enabled: bool = True
    ingress_host: Optional[str] = None
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer"] = "ClusterIP"
    service_port: int = 8080
    health_check_path: str = "/health"
    readiness_check_path: str = "/ready"
    storage_required: bool = False
    storage_size: str = "10Gi"
    autoscaling_enabled: bool = True
    autoscaling_min_replicas: int = 2
    autoscaling_max_replicas: int = 10
    autoscaling_target_cpu: int = 80
    additional_requirements: Optional[str] = None


class ChartPlan(BaseModel):
    """Planned structure for Helm chart"""
    chart_name: str
    chart_version: str = "1.0.0"
    app_version: str
    description: str
    
    # Kubernetes resources to generate
    resources_to_create: List[str] = Field(
        description="List of K8s resources: Deployment, Service, Ingress, etc."
    )
    
    # Dependencies
    chart_dependencies: List[Dict[str, str]] = Field(default_factory=list)
    
    # Configuration structure
    values_structure: Dict = Field(
        description="Structure of values.yaml with defaults"
    )
    
    # Security policies
    security_policies: List[str] = Field(default_factory=list)
    
    # Best practices to apply
    bitnami_compliance: List[str] = Field(
        description="Bitnami standards to implement"
    )
    
    generation_todos: List[str] = Field(
        description="Todo list for generation phase"
    )

class ValidationResult(BaseModel):
    """Result of validation checks"""
    validator: str
    passed: bool
    severity: Literal["info", "warning", "error", "critical"]
    message: str
    details: Optional[Dict] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SecurityScanReport(BaseModel):
    """Security scan results"""
    vulnerabilities: List[Dict] = Field(default_factory=list)
    policy_violations: List[Dict] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=100.0)
    passed: bool
    recommendations: List[str] = Field(default_factory=list)

class ApprovalStatus(BaseModel):
    """Human approval status"""
    status: Literal["pending", "approved", "rejected", "modified"]
    reviewer: Optional[str] = None
    comments: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ArgoCDConfig(BaseModel):
    """ArgoCD Application configuration"""
    application_name: str
    project: str = "default"
    repo_url: str
    target_revision: str = "HEAD"
    path: str
    destination_server: str = "https://kubernetes.default.svc"
    destination_namespace: str
    sync_policy: Dict = Field(default_factory=dict)
    sync_waves: List[Dict] = Field(default_factory=list)


class WorkflowMetadata(BaseModel):
    """Workflow execution metadata"""
    workflow_id: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    current_phase: str
    total_phases: int = 5
    retry_count: int = 0
    max_retries: int = 3


class ErrorContext(BaseModel):
    """Error tracking context"""
    error_type: str
    error_message: str
    failed_node: str
    failed_swarm: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    recoverable: bool = True
    stack_trace: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SupervisorWorkflowState(BaseModel):
    """
    Workflow state tracking for Helm chart generation supervisor.
    
    This schema provides:
    - Type-safe workflow progress tracking aligned with K8s Autopilot phases
    - Automatic phase transitions and completion detection
    - Loop prevention and error detection
    - Integration with langgraph-supervisor and HITL gates
    """
    
    # Current workflow phase (aligned with active_phase in MainSupervisorState)
    current_phase: Literal[
        "requirements",
        "planning",
        "generation",
        "validation",
        "error",
        "complete"
    ] = Field(default="requirements", description="Current workflow phase")
    
    # Phase completion tracking
    planning_complete: bool = Field(default=False, description="Planning phase complete (planning_output exists)")
    generation_complete: bool = Field(default=False, description="Generation phase complete (generated_artifacts exists)")
    validation_complete: bool = Field(default=False, description="Validation phase complete (validation_results populated)")
    
    # HITL approval tracking (mirrors human_approval_status)
    planning_approved: bool = Field(default=False, description="Planning approved by human")
    generation_approved: bool = Field(default=False, description="Generation (Artifacts) approved by human")

    
    # Workflow control
    workflow_complete: bool = Field(default=False, description="Overall workflow complete")
    loop_counter: int = Field(default=0, ge=0, le=30, description="Loop counter for infinite loop prevention")
    last_phase_transition: Optional[datetime] = Field(default=None, description="Timestamp of last phase transition")
    error_occurred: bool = Field(default=False, description="Whether an error occurred")
    error_message: Optional[str] = Field(default=None, description="Error message if any")
    
    # Agent/swarm handoff tracking
    last_swarm: Optional[str] = Field(default=None, description="Last swarm that completed")
    next_swarm: Optional[str] = Field(default=None, description="Next swarm to invoke")
    handoff_reason: Optional[str] = Field(default=None, description="Reason for handoff")
    
    # Workflow metadata
    workflow_id: str = Field(default="", description="Unique workflow identifier")
    started_at: datetime = Field(default_factory=datetime.utcnow, description="Workflow start time")
    
    @property
    def is_complete(self) -> bool:
        """Check if all required phases are complete."""
        # For Helm chart generation: planning → generation → validation
        return all([
            self.planning_complete,
            self.planning_approved,
            self.generation_complete,
            self.generation_approved,
            self.validation_complete
        ])
    
    @property
    def next_phase(self) -> Optional[str]:
        """Determine the next phase based on completion status and approvals."""
        if not self.planning_complete:
            return "planning"
        elif self.planning_complete and not self.planning_approved:
            return "planning"  # Wait for approval
        elif not self.generation_complete:
            return "generation"
        elif self.generation_complete and not self.generation_approved:
            return "generation"  # Wait for artifact approval
        elif not self.validation_complete:
            return "validation"
        else:
            return "complete"
    
    def increment_loop_counter(self) -> None:
        """Increment loop counter and check for limits."""
        self.loop_counter += 1
        if self.loop_counter > 30:
            self.error_occurred = True
            self.error_message = "Maximum iterations reached (30) - possible infinite loop"
    
    def set_phase_complete(self, phase: str) -> None:
        """Mark a specific phase as complete and update state."""
        if phase == "planning":
            self.planning_complete = True
            self.current_phase = "planning"
        elif phase == "generation":
            self.generation_complete = True
            self.current_phase = "generation"
        elif phase == "validation":
            self.validation_complete = True
            self.current_phase = "validation"
        
        # Update transition timestamp
        self.last_phase_transition = datetime.now(timezone.utc)
        
        # Check if workflow is complete
        if self.is_complete:
            self.workflow_complete = True
            self.current_phase = "complete"
    
    def set_approval(self, approval_type: Literal["planning", "generation"], approved: bool) -> None:
        """Set human approval status for a phase."""
        if approval_type == "planning":
            self.planning_approved = approved
        elif approval_type == "generation":
            self.generation_approved = approved
        
        self.last_phase_transition = datetime.now(timezone.utc)
    
    def set_swarm_handoff(self, from_swarm: str, to_swarm: str, reason: str) -> None:
        """Track swarm handoff for debugging and monitoring."""
        self.last_swarm = from_swarm
        self.next_swarm = to_swarm
        self.handoff_reason = reason
        self.last_phase_transition = datetime.now(timezone.utc)
        self.increment_loop_counter()
    
    def get_workflow_progress(self) -> Dict[str, Any]:
        """Get current workflow progress for monitoring."""
        return {
            "current_phase": self.current_phase,
            "planning_complete": self.planning_complete,
            "planning_approved": self.planning_approved,
            "generation_complete": self.generation_complete,
            "generation_approved": self.generation_approved,
            "validation_complete": self.validation_complete,
            "workflow_complete": self.workflow_complete,
            "loop_counter": self.loop_counter,
            "last_swarm": self.last_swarm,
            "next_swarm": self.next_swarm,
            "handoff_reason": self.handoff_reason,
            "error_occurred": self.error_occurred,
            "error_message": self.error_message,
            "is_complete": self.is_complete,
            "next_phase_suggestion": self.next_phase
        }


class WorkflowStatus(str, Enum):
    """Workflow status enumeration."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_APPROVAL = "human_approval"
    INPUT_REQUIRED = "input_required"
    ROLLED_BACK = "rolled_back"
    INTERRUPTED = "interrupted"

class MainSupervisorState(AgentState):
    """
    State schema for the main supervisor agent.
    
    Uses Annotated types with reducers for proper concurrent update handling.
    NotRequired fields are populated during workflow execution by nodes.
    """
    # Required fields at invocation
    messages: Annotated[List[AnyMessage], add_messages]
    llm_input_messages: Annotated[List[AnyMessage], add_messages]
    
    # Deep Agent compatibility (required if supervisor uses Deep Agent features)
    remaining_steps: Annotated[Optional[int], lambda x, y: y]  # Required by Deep Agent TodoListMiddleware
    
    # Core workflow data - initialized with defaults or populated by nodes
    user_query: NotRequired[str]
    user_requirements: NotRequired[ChartRequirements]
    active_phase: NotRequired[Literal[
        "requirements", 
        "planning", 
        "generation", 
        "validation", 
        "error"
    ]]
    
    # Phase outputs - populated during workflow execution
    # Phase outputs - populated during workflow execution
    planner_output:  NotRequired[Dict[str, Any]]
    helm_chart_artifacts: NotRequired[Dict[str, str]]  # filepath -> content
    validation_results: Annotated[List[ValidationResult], add]
    
    # HITL tracking - initialized with defaults
    human_approval_status: NotRequired[Dict[str, ApprovalStatus]]
    
    # HITL: Use Case 1 - General feedback requests during execution
    pending_feedback_requests: NotRequired[Dict[str, Any]]
    # Structure: {
    #   "feedback_id": {
    #     "question": str,
    #     "context": Dict[str, Any],
    #     "phase": str,
    #     "timestamp": datetime,
    #     "status": "pending" | "answered"
    #   }
    # }
    
    # HITL: Use Case 2 - Review tool call results after execution
    tool_call_results_for_review: NotRequired[Dict[str, Any]]
    # Structure: {
    #   "tool_call_id": {
    #     "tool_name": str,
    #     "tool_args": Dict[str, Any],
    #     "tool_result": Any,
    #     "phase": str,
    #     "requires_review": bool,
    #     "review_status": "pending" | "approved" | "rejected" | "modified"
    #   }
    # }
    
    # HITL: Use Case 4 - Critical tool call pre-approval (before execution)
    pending_tool_calls: NotRequired[Dict[str, Any]]
    # Structure: {
    #   "tool_call_id": {
    #     "tool_name": str,
    #     "tool_args": Dict[str, Any],
    #     "is_critical": bool,
    #     "phase": str,
    #     "reason": str,  # Why approval is needed
    #     "status": "pending" | "approved" | "rejected" | "modified"
    #   }
    # }
    
    tool_call_approvals: NotRequired[Dict[str, ApprovalStatus]]
    # Maps tool_call_id -> ApprovalStatus for critical tool calls
    
    # Workflow state tracking - initialized at start
    workflow_state: NotRequired[SupervisorWorkflowState]

    status: WorkflowStatus = WorkflowStatus.PENDING
    
    # Error tracking - populated on errors
    error_state: NotRequired[ErrorContext]
    
    # File system artifacts (for Deep Agents) - populated during generation
    file_artifacts: NotRequired[Annotated[Dict[str, str], lambda x, y: {**(x or {}), **(y or {})}]]

    session_id: NotRequired[str] 
    task_id: NotRequired[str]
    # Todo tracking - populated during workflow
    todos: Annotated[List[Dict], add]
    
    # Workspace directory for chart generation/validation (set during security review)
    workspace_dir: NotRequired[str]  # Default: "/tmp/helm-charts"


class PlanningSwarmState(AgentState):
    """State for Planning Swarm (Deep Agent-based)"""
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Deep Agent required fields (must be present for create_deep_agent to work)
    remaining_steps: Annotated[Optional[int], lambda x, y: y]  # Required by Deep Agent TodoListMiddleware
    
    # Active agent tracking
    active_agent: Annotated[Optional[Literal[
        "requirement_analyzer",
        "architecture_planner"
    ]], lambda x, y: y]
    
    # Inputs from main supervisor
    user_query: NotRequired[str]
    updated_user_requirements: NotRequired[str]
    question_asked: NotRequired[str]
    # Use Union[str, WorkflowStatus] to allow both enum and string values
    # This avoids Pydantic serialization warnings when enum gets serialized to string
    status: NotRequired[str | WorkflowStatus]  # Default handled by type checker
    session_id: NotRequired[str] 
    task_id: NotRequired[str]
    # Phase outputs
    # NotRequired already makes field optional, so Optional is redundant
    requirements_validation: NotRequired[ValidationResult]
    research_findings: NotRequired[Annotated[List[Dict[str, Any]], add]]
    # Overall Planned Chart Structure
    chart_plan: NotRequired[Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]]
    
    # Deep Agent features
    todos: NotRequired[Annotated[List[Dict[str, Any]], add]]
    workspace_files: NotRequired[Annotated[Dict[str, str], lambda x, y: {**(x or {}), **(y or {})}]]
    
    # Handoff context
    handoff_data: NotRequired[Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]]
    
    # HITL: Use Case 1 - General feedback requests during execution
    pending_feedback_requests: NotRequired[Dict[str, Any]]
    
    # HITL: Use Case 2 - Review tool call results after execution
    tool_call_results_for_review: NotRequired[Dict[str, Any]]
    
    # HITL: Use Case 4 - Critical tool call pre-approval (before execution)
    pending_tool_calls: NotRequired[Dict[str, Any]]
    

# ============================================================================
# Generation Swarm State
# ============================================================================

class GenerationSwarmState(AgentState):
    """State for Generation Swarm (Deep Agent-based)"""
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Deep Agent required fields (must be present for create_deep_agent to work)
    remaining_steps: Annotated[Optional[int], lambda x, y: y]  # Required by Deep Agent TodoListMiddleware
    # Inputs from planning swarm
    planner_output: NotRequired[Dict[str, Any]]
    
    # Generated artifacts (using merge reducer for concurrent updates)
    generated_templates: NotRequired[Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]]
    validation_results: NotRequired[Annotated[List[Any], add]]
    template_variables: NotRequired[Annotated[List[str], add]]
    
    generation_status: NotRequired[Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]]
    
    # Execution State (Coordinator Architecture)
    current_phase: NotRequired[Literal[
        "INIT",
        "CORE_TEMPLATES",
        "CONDITIONAL_TEMPLATES",
        "HELPERS_AND_CONFIG",
        "DOCUMENTATION",
        "AGGREGATION",
        "COMPLETED"
    ]]
    
    next_action: NotRequired[str]  # Name of next tool or agent action
    
    tools_to_execute: NotRequired[List[str]]  # Queue of pending tools
    completed_tools: NotRequired[List[str]]  # Executed tools (ordered)
    pending_dependencies: NotRequired[Dict[str, List[str]]]  # Tool: [dependencies]
    
    # Tool Results
    tool_results: NotRequired[Dict[str, Any]]
    
    # Coordinator State
    coordinator_state: NotRequired[Dict[str, Any]]
    
    # Error Handling
    errors: NotRequired[List[Dict[str, Any]]]
    
    # Final Output
    final_helm_chart: NotRequired[Dict[str, Any]]
    final_status: NotRequired[Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILED", None]]
    
    session_id: NotRequired[str] 
    task_id: NotRequired[str]
    # Deep Agent features
    todos: NotRequired[Annotated[List[Dict[str, Any]], add]]
    workspace_files: NotRequired[Annotated[Dict[str, str], lambda x, y: {**(x or {}), **(y or {})}]]
    
    # Generation metadata
    generation_metadata: NotRequired[Dict[str, Any]]
    handoff_data: NotRequired[Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]]


# ============================================================================
# Validation & Deployment Swarm State
# ============================================================================

class ValidationSwarmState(AgentState):
    """State for Validation & Deployment Swarm"""
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Deep Agent required fields (if using Deep Agent for validation)
    remaining_steps: Annotated[Optional[int], lambda x, y: y]  # Required by Deep Agent TodoListMiddleware
    
    # Active agent tracking
    active_agent: Annotated[Optional[Literal[
        "chart_validator",
        "security_scanner",
        "test_generator",
        "argocd_configurator",
        "validation_supervisor"
    ]], lambda x, y: y]
    
    # Inputs from generation swarm
    generated_chart: NotRequired[Dict[str, str]]
    validation_status: NotRequired[Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]]
    # Validation results (can be updated concurrently)
    validation_results: NotRequired[Annotated[List[ValidationResult], add]]
    security_scan_results: NotRequired[Optional[SecurityScanReport]]
    test_artifacts: NotRequired[Optional[Dict[str, str]]]
    argocd_manifests: NotRequired[Optional[ArgoCDConfig]]
    
    # Deployment readiness
    deployment_ready: NotRequired[bool]
    blocking_issues: NotRequired[Annotated[List[str], add]]
    
    # Retry tracking (prevents infinite retry loops)
    # Uses merge reducer to allow concurrent updates from multiple validators
    validation_retry_counts: NotRequired[Annotated[Dict[str, int], lambda x, y: {**(x or {}), **(y or {})}]]  # validator_name -> retry_count

    session_id: NotRequired[str] 
    task_id: NotRequired[str]
    # Deep Agent features
    todos: NotRequired[Annotated[List[Dict[str, Any]], add]]
    # Handoff context
    handoff_metadata: NotRequired[Dict]


