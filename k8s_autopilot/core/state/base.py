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


# ============================================================================
# ArgoCD Onboarding Agent State
# ============================================================================

class ApprovalRecord(TypedDict, total=False):
    """Approval checkpoint record"""
    checkpoint_id: str
    type: Literal[
        "plan_approval",
        "project_approval",
        "repo_approval",
        "app_approval",
        "sync_approval",
        "delete_approval",
    ]
    approved: bool
    approved_by: Optional[str]
    approved_at: Optional[str]
    feedback: Optional[str]


class ArgoCDOnboardingState(AgentState):
    """
    State schema for ArgoCD Application Onboarding Deep Agent.
    
    Follows the HelmAgentState pattern with:
    - Message history (auto-managed by LangGraph)
    - Workflow type and phase tracking
    - ArgoCD-specific state (projects, repos, apps)
    - Approval checkpoints (HITL)
    - Audit trail
    - Error handling
    """
    # Message history (auto-managed by LangGraph)
    messages: Annotated[List[AnyMessage], add_messages]
    
    # User request information
    user_request: str
    user_id: str
    session_id: str
    
    # Workflow type and phase
    workflow_type: Annotated[Literal["onboarding", "offboarding", "debug", "query"], lambda x, y: y]
    current_phase: Annotated[int, lambda x, y: y]  # 1-5 phase workflow
    
    # Cluster context
    cluster_context: Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]
    
    # ==================== ArgoCD Project State ====================
    project_info: Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]
    project_list: Annotated[List[dict], add]
    project_created: Annotated[bool, lambda x, y: y]
    project_validation_result: Annotated[Optional[Dict], lambda x, y: y]
    
    # ==================== ArgoCD Repository State ====================
    repository_info: Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]
    repository_list: Annotated[List[dict], add]
    repo_validation_result: Annotated[Optional[Dict], lambda x, y: y]
    repo_onboarded: Annotated[bool, lambda x, y: y]
    
    # ==================== ArgoCD Application State ====================
    application_info: Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]
    application_list: Annotated[List[dict], add]
    application_created: Annotated[bool, lambda x, y: y]
    application_details: Annotated[Optional[Dict], lambda x, y: y]
    
    # ==================== Deployment/Sync State ====================
    sync_operation_id: Annotated[Optional[str], lambda x, y: y]
    deployment_status: Annotated[Optional[Dict], lambda x, y: y]
    health_report: Annotated[Optional[Dict], lambda x, y: y]
    sync_status: Annotated[Literal["pending", "in_progress", "synced", "out_of_sync", "failed", None], lambda x, y: y]
    
    # ==================== Debug Results ====================
    debug_results: Annotated[Dict[str, Any], lambda x, y: {**(x or {}), **(y or {})}]
    logs_collected: Annotated[List[str], add]
    events_collected: Annotated[List[dict], add]
    metrics_collected: Annotated[Optional[Dict], lambda x, y: y]
    
    # ==================== Approvals (HITL Checkpoints) ====================
    approval_checkpoints: Annotated[List[ApprovalRecord], add]
    pending_approval: Annotated[bool, lambda x, y: y]
    approval_status: Annotated[Literal["pending", "approved", "rejected", "modifications_requested"], lambda x, y: y]
    # Phase-specific approvals
    checkpoint_1_approved: Annotated[bool, lambda x, y: y]  # Project/Repo config
    checkpoint_2_approved: Annotated[bool, lambda x, y: y]  # Application creation
    checkpoint_3_approved: Annotated[bool, lambda x, y: y]  # Sync to cluster
    checkpoint_4_approved: Annotated[bool, lambda x, y: y]  # Production deployment
    
    # ==================== Audit Trail ====================
    audit_log: Annotated[List[dict], add]
    execution_logs: Annotated[List[dict], add]  # Tool execution logs
    
    # ==================== Error Handling ====================
    errors: Annotated[List[str], add]
    warnings: Annotated[List[str], add]
    last_error: Annotated[Optional[str], lambda x, y: y]
    error_count: Annotated[int, add]
    
    # ==================== Deep Agent Compatibility ====================
    remaining_steps: Annotated[Optional[int], lambda x, y: y]  # For TodoListMiddleware
    _seen_tool_calls: Annotated[List[str], add]  # For de-dup tracking (Issue #7)

    
    # ==================== Request Classification ====================
    request_classification: Annotated[Optional[Dict], lambda x, y: y]
    request_type: Annotated[Literal["workflow", "query", "unknown"], lambda x, y: y]
    operation_name: Annotated[str, lambda x, y: y]
    
    # ==================== Query Results ====================
    query_results: Annotated[List[dict], add]
    query_formatted_response: Annotated[str, lambda x, y: y]


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
    planning_complete: bool = Field(default=False)
    generation_complete: bool = Field(default=False)
    validation_complete: bool = Field(default=False)
    helm_mgmt_complete: bool = Field(default=False)
    argocd_onboarding_complete: bool = Field(default=False)
    k8s_cluster_ops_complete: bool = Field(default=False)
    app_operator_complete: bool = Field(default=False)
    observability_complete: bool = Field(default=False)
    
    # HITL approval tracking
    planning_approved: bool = Field(default=False)
    generation_approved: bool = Field(default=False)
    
    # Workflow control
    workflow_complete: bool = Field(default=False, description="Overall workflow complete")
    loop_counter: int = Field(default=0, ge=0, le=50, description="Loop counter for infinite loop prevention")
    error_occurred: bool = Field(default=False)
    error_message: Optional[str] = Field(default=None)
    
    def set_phase_complete(self, phase: str) -> None:
        """Mark a specific phase as complete."""
        mapping = {
            "planning": "planning_complete",
            "generation": "generation_complete",
            "validation": "validation_complete",
            "helm_mgmt": "helm_mgmt_complete",
            "argocd_onboarding": "argocd_onboarding_complete",
            "k8s_cluster_ops": "k8s_cluster_ops_complete",
            "app_operator": "app_operator_complete",
            "observability_operator": "observability_complete",
        }
        attr = mapping.get(phase)
        if attr:
            setattr(self, attr, True)
            self.last_agent = phase
        
        # Check if workflow is complete
        if self._is_complete():
            self.workflow_complete = True
            self.current_phase = "complete"
    
    def set_approval(self, approval_type: str, approved: bool) -> None:
        """Set human approval status for a phase."""
        mapping = {
            "planning": "planning_approved",
            "generation": "generation_approved",
        }
        attr = mapping.get(approval_type)
        if attr:
            setattr(self, attr, approved)
    
    def _is_complete(self) -> bool:
        """Check if all required phases are complete."""
        return all([
            self.planning_complete,
            self.planning_approved,
            self.generation_complete,
            self.generation_approved,
            self.validation_complete,
        ])
    
    def get_workflow_progress(self) -> Dict[str, Any]:
        """Get current workflow progress for monitoring."""
        return {
            "current_phase": self.current_phase,
            "last_agent": self.last_agent,
            "next_agent": self.next_agent,
            "planning_complete": self.planning_complete,
            "planning_approved": self.planning_approved,
            "generation_complete": self.generation_complete,
            "generation_approved": self.generation_approved,
            "validation_complete": self.validation_complete,
            "helm_mgmt_complete": self.helm_mgmt_complete,
            "argocd_complete": self.argocd_onboarding_complete,
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
    ROLLED_BACK = "rolled_back"
    INTERRUPTED = "interrupted"

class MainSupervisorState(AgentState):
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
    user_query: NotRequired[str]
    session_id: NotRequired[str]
    task_id: NotRequired[str]
    
    # ── Runtime context (injected into deep-agent config) ─────────────
    context: NotRequired[Dict[str, Any]]
    
    # ── Workflow tracking ─────────────────────────────────────────────
    status: NotRequired[str]  # "pending" | "working" | "completed" | "error"
    current_phase: NotRequired[str]
    workflow_state: NotRequired[SupervisorWorkflowState]
    workflow_complete: NotRequired[bool]
    
    # ── Deep agent coordinator outputs (one per coordinator) ──────────
    helm_operator_output: NotRequired[Dict[str, Any]]
    app_operator_output: NotRequired[Dict[str, Any]]
    k8s_operator_output: NotRequired[Dict[str, Any]]
    observability_output: NotRequired[Dict[str, Any]]
    
    # ── Cross-domain context (for coordinator-to-coordinator handoffs) ─
    cross_domain_context: NotRequired[Dict[str, Any]]
    
    # ── Domain summaries (blackboard for cross-domain awareness) ──────
    domain_summaries: Annotated[List[Dict[str, Any]], operator.add]
    
    # ── HITL ──────────────────────────────────────────────────────────
    pending_feedback_requests: NotRequired[Dict[str, Any]]


# ============================================================================
# Helm Planner State (K8s Operator — 2-phase pipeline)
# Canonical definitions live in k8s_autopilot.core.state.helm_planner_state
# Re-exported here for backward compatibility.
# ============================================================================

from k8s_autopilot.core.state.helm_planner_state import (  # noqa: F401
    HelmPlannerWorkflowState,
    HelmPlannerState,
)


class K8sOperatorContext(BaseModel):
    """Runtime context for HelmOperatorCoordinator.

    Injected via ``config["configurable"]["context"]`` during deep agent invocation.
    Reference: aws-orchestrator TFCoordinatorContext
    """
    session_id: str = Field(default="", description="A2A session/context ID")
    task_id: str = Field(default="", description="A2A task ID")
    org_name: str = Field(default="default_org", description="Organization namespace for memory scoping")
    workspace_dir: str = Field(default="/tmp/helm-charts", description="Physical workspace for generated charts")


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


class HelmChartMetadata(TypedDict, total=False):
    """Metadata about the Helm chart being installed"""
    name: str
    repository: str
    repository_url: str
    version: str
    description: str
    app_version: str
    maintainers: list[dict]
    dependencies: list[dict]
    values_schema: dict  # JSON Schema from values.schema.json
    values_schema_required_fields: list[str]
    example_values: dict
    documentation_url: str
    icons: list[str]

class ValidationError(TypedDict):
    """Schema for validation errors"""
    field: str
    error_message: str
    required: bool
    provided_value: str | None
    expected_type: str
    severity: Literal["critical", "warning"]

class InstallationPlan(TypedDict, total=False):
    """Structured plan for installation"""
    chart_name: str
    version: str
    namespace: str
    release_name: str
    values: dict
    plan_steps: list[str]
    prerequisites_check: dict
    estimated_resources: dict  # CPU, Memory, Storage
    rollback_strategy: str
    monitoring_plan: dict
    created_at: str
    status: Literal["draft", "pending_approval", "approved", "rejected"]

class ExecutionLog(TypedDict, total=False):
    """Execution tracking"""
    step: int
    timestamp: str
    action: str
    status: Literal["pending", "running", "success", "failed", "skipped"]
    output: str
    error: str | None
    duration_ms: int

class ApprovalCheckpoint(TypedDict, total=False):
    """Human approval state"""
    checkpoint_id: str
    type: Literal["plan_review", "tool_execution", "values_confirmation"]
    created_at: str
    approved: bool | None
    approved_by: str | None
    approved_at: str | None
    feedback: str
    required_changes: list[str]

class RequestClassification(BaseModel):
    """Structured classification of user request for routing"""
    intent_type: Literal["workflow", "query", "unclear"] = Field(
        description="Type of request: workflow (state-changing) or query (read-only)"
    )
    operation: Literal[
        # Workflow operations
        "install",
        "upgrade",
        "rollback",
        "uninstall",
        # Query operations
        "list_releases",
        "get_release_status",
        "get_chart_info",
        "search_charts",
        "list_namespaces",
        "list_charts",
        "describe_release",
        "get_values",
        # Unknown
        "unknown",
    ] = Field(description="Specific operation requested")
    requires_approval: bool = Field(
        description="Does operation need human approval?"
    )
    confidence_level: Literal["high", "medium", "low"] = Field(
        description="Confidence in classification"
    )
    reasoning: str = Field(
        description="Brief explanation of classification"
    )

class HelmAgentState(AgentState):
    """Master state schema for the agent"""
    # Message history (auto-managed by LangGraph)
    messages: Annotated[List[AnyMessage], add_messages]
    
    # User request information
    user_request: str
    user_id: str
    session_id: str
    cluster_context: Annotated[dict, lambda x, y: {**(x or {}), **(y or {})}]  # Merge context
    
    # Chart discovery phase
    chart_metadata: Annotated[HelmChartMetadata, lambda x, y: y] # Overwrite
    chart_search_results: Annotated[list[dict], add] # Append results
    
    # Values and configuration
    user_provided_values: Annotated[dict, lambda x, y: {**(x or {}), **(y or {})}]
    merged_values: Annotated[dict, lambda x, y: {**(x or {}), **(y or {})}]
    validation_errors: Annotated[list[ValidationError], add]
    validation_status: Literal["pending", "in_progress", "passed", "failed"]
    
    # Planning phase
    installation_plan: Annotated[InstallationPlan, lambda x, y: y]
    plan_validation_results: dict
    prerequisites_check_results: dict
    
    # Approval phase
    approval_checkpoints: Annotated[list[ApprovalCheckpoint], add]
    pending_approval: bool
    approval_status: Literal["pending", "approved", "rejected", "modifications_requested"]
    
    # Execution phase
    execution_started_at: str | None
    execution_status: Annotated[Literal["not_started", "in_progress", "completed", "failed", "rolled_back"], lambda x, y: y]
    execution_logs: Annotated[list[ExecutionLog], add]
    helm_release_name: Annotated[str | None, lambda x, y: y]
    helm_release_namespace: Annotated[str | None, lambda x, y: y]
    
    # Monitoring and rollback
    deployment_status: Annotated[dict, lambda x, y: {**(x or {}), **(y or {})}]
    rollback_available: bool
    rollback_executed: bool
    
    # Error tracking
    last_error: Annotated[str | None, lambda x, y: y]
    error_count: Annotated[int, add]
    recovery_attempts: Annotated[list[dict], add]
    
    # Request classification (for dual-path routing)
    request_classification: Annotated[Optional[RequestClassification], lambda x, y: y]
    request_type: Annotated[Literal["workflow", "query", "unknown"], lambda x, y: y]
    operation_name: Annotated[str, lambda x, y: y]
    
    # Query-specific fields (for read-only operations)
    query_results: Annotated[list[dict], add]
    query_formatted_response: Annotated[str, lambda x, y: y]
    
    # Audit trail
    audit_log: Annotated[list[dict], add]
