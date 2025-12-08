from pydantic import BaseModel, Field, field_validator, ValidationInfo
from enum import Enum
from typing import Dict, Optional, Literal, List, Any, Union
import json
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage
from k8s_autopilot.core.state.base import PlanningSwarmState
from typing_extensions import Annotated
from toon import encode
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .planner_analyzer_prompts import (
    ANALYZE_APPLICATION_REQUIREMENTS_HUMAN_PROMPT,
    ANALYZE_APPLICATION_REQUIREMENTS_SYSTEM_PROMPT,
    DESIGN_KUBERNETES_ARCHITECTURE_HUMAN_PROMPT,
    DESIGN_KUBERNETES_ARCHITECTURE_SYSTEM_PROMPT,
    ESTIMATE_RESOURCES_HUMAN_PROMPT,
    ESTIMATE_RESOURCES_SYSTEM_PROMPT,
    DEFINE_SCALING_STRATEGY_HUMAN_PROMPT,
    DEFINE_SCALING_STRATEGY_SYSTEM_PROMPT,
    CHECK_DEPENDENCIES_HUMAN_PROMPT,
    CHECK_DEPENDENCIES_SYSTEM_PROMPT
)
from .ingress_analyzer_schema import IngressRouteGenerationPlannerInput

# Create agent logger for planner parser
planner_parser_logger = AgentLogger("k8sAutopilotPlannerParser")


class FrameworkAnalysis(BaseModel):
    """Framework-specific analysis results."""
    
    startup_time_seconds: int = Field(
        ...,
        description="Expected application startup time in seconds",
        ge=1,
        le=300
    )
    typical_memory_mb: int = Field(
        ...,
        description="Typical memory consumption in megabytes during normal operation",
        ge=64,
        le=16384
    )
    cpu_cores: float = Field(
        ...,
        description="Typical CPU cores required (can be fractional, e.g., 0.5)",
        ge=0.1,
        le=16.0
    )
    connection_pooling_needed: bool = Field(
        ...,
        description="Whether the application requires connection pooling for databases or external services"
    )
    graceful_shutdown_period: int = Field(
        ...,
        description="Required graceful shutdown period in seconds to properly close connections",
        ge=5,
        le=120
    )
    liveness_probe_path: Optional[str] = Field(
        default=None,
        description="Endpoint for liveness probe (if applicable)"
    )
    readiness_probe_path: Optional[str] = Field(
        default=None,
        description="Endpoint for readiness probe (if applicable)"
    )
    initial_delay_seconds: Optional[int] = Field(
        default=None,
        description="Initial delay before probes start",
        ge=0,
        le=300
    )

class ScalabilityAnalysis(BaseModel):
    """Scalability characteristics of the application."""
    
    horizontally_scalable: bool = Field(
        ...,
        description="Whether the application can scale horizontally by adding more instances"
    )
    stateless: bool = Field(
        ...,
        description="Whether the application is stateless (can handle requests without persistent local state)"
    )
    session_affinity_needed: bool = Field(
        ...,
        description="Whether session affinity (sticky sessions) is required for proper operation"
    )
    load_balancing_algorithm: Literal["round-robin", "least-connections", "ip-hash", "random"] = Field(
        ...,
        description="Recommended load balancing algorithm based on application characteristics"
    )
    # NEW: Autoscaling configuration
    hpa_enabled: bool = Field(
        ...,
        description="Whether HPA should be configured"
    )
    target_cpu_utilization: Optional[int] = Field(
        None,
        ge=30,
        le=90,
        description="Target CPU % for autoscaling"
    )
    target_memory_utilization: Optional[int] = Field(
        None,
        ge=30,
        le=90,
        description="Target memory % for autoscaling"
    )

class StorageAnalysis(BaseModel):
    """Storage requirements analysis."""
    
    temp_storage_needed: bool = Field(
        ...,
        description="Whether temporary/ephemeral storage is needed (e.g., for caching, temp files)"
    )
    persistent_storage: bool = Field(
        ...,
        description="Whether persistent storage is required for data that must survive pod restarts"
    )
    volume_size_gb: Optional[int] = Field(
        None,
        description="Required persistent volume size in gigabytes if persistent_storage is True",
        ge=1,
        le=1000
    )

class NetworkingAnalysis(BaseModel):
    """Networking configuration analysis."""
    
    port: int = Field(
        ...,
        description="Primary port on which the application listens",
        ge=1,
        le=65535
    )
    protocol: Literal["http", "https", "tcp", "udp", "grpc"] = Field(
        ...,
        description="Primary protocol used by the application"
    )
    tls_needed: bool = Field(
        ...,
        description="Whether TLS/SSL encryption is required for secure communication"
    )

class CoreResource(BaseModel):
    """Core Kubernetes resource definition."""
    
    type: Literal["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Namespace"] = Field(
        ...,
        description="Type of core Kubernetes workload resource"
    )
    alternatives_considered: List[str] = Field(
        default_factory=list,
        description="Other resource types considered and why they were rejected",
        max_items=3
    )
    key_configuration_parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Critical configuration parameters for this resource (replicas, update strategy, etc.)"
    )

class AuxiliaryResource(BaseModel):
    """Auxiliary Kubernetes resource definition."""
    
    type: Literal[
        "Service",
        "ConfigMap",
        "Secret",
        "HorizontalPodAutoscaler",
        "VerticalPodAutoscaler",
        "PodDisruptionBudget",
        "NetworkPolicy",
        "Ingress",
        "ServiceAccount",
        "PersistentVolumeClaim",
        "ResourceQuota",
        "LimitRange"
    ] = Field(
        ...,
        description="Type of auxiliary Kubernetes resource"
    )
    criticality: Literal["essential", "production-critical", "recommended", "optional"] = Field(
        ...,
        description="How critical this resource is for the deployment"
    )
    
    configuration_hints: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Key configuration parameters or values for this resource"
    )
    
    dependencies: List[str] = Field(
        default_factory=list,
        description="Other resources this depends on",
        max_items=5
    )
    
    environment_specific: Optional[str] = Field(
        default=None,
        description="If this resource is specific to certain environments (prod/staging/dev)"
    )
    
    tradeoffs: Optional[str] = Field(
        default=None,
        description="Any tradeoffs or considerations when using this resource",
    )

class ResourcesArchitecture(BaseModel):
    """Complete Kubernetes resources architecture."""
    
    core: List[CoreResource] = Field(
        ...,
        description="List of core resources (Namespace, Deployment, StatefulSet, etc.)",
        min_items=1,
        max_items=5
    )
    auxiliary: List[AuxiliaryResource] = Field(
        ...,
        description="List of auxiliary resources needed to support the application",
        min_items=1,
        max_items=15
    )
    architecture_pattern: Literal[
        "stateless_microservice",
        "stateful_application",
        "batch_processing",
        "system_daemon",
        "custom"
    ] = Field(
        ...,
        description="The overall architecture pattern this design follows"
    )
    
    estimated_complexity: Literal["low", "medium", "high"] = Field(
        ...,
        description="Operational complexity of managing this architecture"
    )

class DesignDecision(BaseModel):
    """Individual architectural decision with context."""
    
    category: Literal[
        "workload_selection",
        "scalability",
        "high_availability",
        "security",
        "networking",
        "storage",
        "observability",
        "cost_optimization",
        "operational_excellence"
    ] = Field(
        ...,
        description="Category of this design decision"
    )
    
    decision: str = Field(
        ...,
        description="The specific decision made",
        min_length=20,
        max_length=500
    )
    
    rationale: str = Field(
        ...,
        description="Why this decision was made, with specific references to requirements",
        min_length=50,
        max_length=500
    )
    
    alternatives: Optional[str] = Field(
        default=None,
        description="Alternative approaches considered and why they were not chosen",
        max_length=300
    )
    
    risk_mitigation: Optional[str] = Field(
        default=None,
        description="How risks associated with this decision are mitigated",
        max_length=300
    )

class ConfigurationAnalysis(BaseModel):
    """Configuration management requirements."""
    
    config_maps_needed: bool
    secrets_needed: bool
    env_vars_count_estimate: int = Field(..., ge=0, le=100)

class SecurityAnalysis(BaseModel):
    """Security context and requirements."""
    
    run_as_non_root: bool = Field(
        default=True,
        description="Whether to enforce non-root user"
    )
    read_only_root_filesystem: bool = Field(
        default=False,
        description="Whether root filesystem should be read-only"
    )
    capabilities_to_drop: list[str] = Field(
        default=["ALL"],
        description="Linux capabilities to drop"
    )
    service_account_needed: bool = Field(
        ...,
        description="Whether custom ServiceAccount is required"
    )

class ResourceSpec(BaseModel):
    """Resource requests and limits specification."""
    
    cpu: str = Field(
        ...,
        description="CPU resource specification (e.g., '100m', '0.5', '1', '2000m')",
        pattern=r"^\d+(\.\d+)?m?$"
    )
    memory: str = Field(
        ...,
        description="Memory resource specification (e.g., '256Mi', '1.5Gi', '512Mi')",
        pattern=r"^\d+(\.\d+)?(Mi|Gi)$"
    )

class EnvironmentResourceSpec(BaseModel):
    """Complete resource specification for an environment."""
    
    requests: ResourceSpec = Field(
        ...,
        description="Minimum guaranteed resources (should be lower than limits)"
    )
    limits: ResourceSpec = Field(
        ...,
        description="Maximum resources the container can use (should be higher than requests)"
    )

    qos_class: Literal["Guaranteed", "Burstable", "BestEffort"] = Field(
        ...,
        description="Kubernetes Quality of Service class based on requests/limits configuration"
    )
    
    expected_utilization: Dict[str, float] = Field(
        ...,
        description="Expected resource utilization percentages",
        example={"cpu": 70.0, "memory": 75.0}
    )
    
    scaling_headroom_percent: float = Field(
        ...,
        ge=10.0,
        le=50.0,
        description="Percentage of headroom for traffic spikes (10-50%)"
    )

class FrameworkSpecificConsiderations(BaseModel):
    """Framework-specific factors that influenced the estimation."""
    
    startup_overhead_mb: int = Field(
        ...,
        description="Additional memory needed during startup phase"
    )
    
    runtime_overhead_mb: int = Field(
        ...,
        description="Framework runtime memory overhead"
    )
    
    concurrent_request_impact: str = Field(
        ...,
        description="How concurrent requests affect resource usage"
    )
    
    garbage_collection_impact: Optional[str] = Field(
        None,
        description="GC impact for JVM/managed runtime languages"
    )
    
    recommended_heap_size: Optional[str] = Field(
        None,
        description="Recommended heap size for JVM apps (e.g., '-Xmx1024m')"
    )

class ResourceEstimationMetadata(BaseModel):
    """Metadata about the estimation process for auditing."""
    
    estimation_methodology: str = Field(
        ...,
        description="Brief description of the estimation approach used"
    )
    
    confidence_level: Literal["low", "medium", "high"] = Field(
        ...,
        description="Confidence in the estimates based on available data"
    )
    
    assumptions: List[str] = Field(
        ...,
        description="Key assumptions made during estimation",
        min_length=1,
        max_length=10
    )
    
    risk_factors: List[str] = Field(
        ...,
        description="Identified risks that may affect resource accuracy",
        max_length=5
    )
    
    monitoring_recommendations: List[str] = Field(
        ...,
        description="Specific metrics to monitor post-deployment",
        min_length=3,
        max_length=8
    )

class HPAConfiguration(BaseModel):
    """Horizontal Pod Autoscaler configuration for a specific environment."""
    
    min_replicas: int = Field(
        ...,
        description="Minimum number of pod replicas (cannot scale below this)",
        ge=1,
        le=100
    )
    max_replicas: int = Field(
        ...,
        description="Maximum number of pod replicas (cannot scale above this)",
        ge=1,
        le=1000
    )
    target_cpu_utilization: int = Field(
        ...,
        description="Target CPU utilization percentage that triggers scaling",
        ge=50,
        le=90
    )
    target_memory_utilization: Optional[int] = Field(
        None,
        description="Optional target memory utilization percentage for memory-based scaling",
        ge=50,
        le=90
    )
    # PDB Configuration
    min_available: Optional[Union[int, str]] = Field(
        None,
        description="Minimum available pods (int or percentage like '50%')"
    )
    max_unavailable: Optional[Union[int, str]] = Field(
        None,
        description="Maximum unavailable pods (int or percentage like '25%')"
    )
    unhealthy_pod_eviction_policy: Optional[Literal["IfHealthyBudget", "AlwaysAllow"]] = Field(
        "IfHealthyBudget",
        description="Policy for evicting unhealthy pods"
    )

class ScalingBehavior(BaseModel):
    """Scaling behavior configuration (K8s 1.18+)"""
    
    # Scale up behavior
    scale_up_stabilization_window_seconds: int = Field(
        0,
        ge=0,
        description="How long to wait before scaling up again"
    )
    scale_up_select_policy: Literal["Max", "Min", "Disabled"] = Field(
        "Max",
        description="Policy for selecting which scaling change to apply"
    )
    scale_up_policies: List[Dict[str, Any]] = Field(
        default=[],
        description="Scaling policies for scale up"
    )
    
    # Scale down behavior
    scale_down_stabilization_window_seconds: int = Field(
        300,
        ge=0,
        description="How long to wait before scaling down (default 5 min)"
    )
    scale_down_select_policy: Literal["Max", "Min", "Disabled"] = Field(
        "Min",
        description="Policy for selecting which scaling change to apply"
    )
    scale_down_policies: List[Dict[str, Any]] = Field(
        default=[],
        description="Scaling policies for scale down"
    )
    
class HelmDependency(BaseModel):
    """Helm chart dependency definition."""
    
    name: str = Field(
        ...,
        description="Name of the Helm chart dependency",
        min_length=1,
        max_length=100
    )
    repository: Optional[str] = Field(
        None,
        description="Helm repository URL where the chart is located"
    )
    version: str = Field(
        ...,
        description="Chart version or version constraint (e.g., '12.x', '^1.0.0', '~2.3.0')",
        min_length=1,
        max_length=50
    )
    condition: Optional[str] = Field(
        None,
        description="Helm conditional flag to enable/disable this dependency (e.g., 'postgresql.enabled')"
    )
    alias: Optional[str] = Field(
        None,
        description="Alternative name for the dependency in values.yaml"
    )
    reason: str = Field(
        ...,
        description="Why this dependency is needed for the application",
        min_length=10,
        max_length=300
    )
    tags: Optional[List[str]] = Field(
        None,
        description="Tags to group dependencies (e.g., ['database', 'production'])"
    )

class InitContainer(BaseModel):
    """Init container definition."""
    
    name: str = Field(
        ...,
        description="Name of the init container",
        pattern=r"^[a-z0-9-]+$"
    )
    image: Optional[str] = Field(
        None,
        description="Container image to use (e.g., 'busybox:latest', 'bitnami/postgresql:14')"
    )
    purpose: str = Field(
        ...,
        description="What this init container does (e.g., database migration, wait-for-service)",
        min_length=10,
        max_length=200
    )
    estimated_duration_seconds: Optional[int] = Field(
        None,
        description="Expected execution time in seconds",
        ge=1,
        le=600
    )
    retry_policy: Optional[Literal["Never", "OnFailure"]] = Field(
        "Never",
        description="Restart policy for the init container"
    )


class Sidecar(BaseModel):
    """Sidecar container definition."""
    
    name: str = Field(
        ...,
        description="Name of the sidecar container",
        pattern=r"^[a-z0-9-]+$"
    )
    image: Optional[str] = Field(
        None,
        description="Container image to use (e.g., 'fluent/fluent-bit:latest')"
    )
    purpose: str = Field(
        ...,
        description="What this sidecar does (e.g., log shipping, service mesh proxy, metrics collection)",
        min_length=10,
        max_length=200
    )
    communication_type: Optional[Literal["shared-volume", "localhost-network", "none"]] = Field(
        None,
        description="How the sidecar communicates with the main container"
    )
    resource_impact: Optional[Literal["low", "medium", "high"]] = Field(
        None,
        description="Expected CPU/memory overhead of this sidecar"
    )

class HelmHook(BaseModel):
    """Helm hook definition with job specification."""
    
    hook_type: Literal[
        "pre-install",
        "post-install",
        "pre-delete",
        "post-delete",
        "pre-upgrade",
        "post-upgrade",
        "pre-rollback",
        "post-rollback",
        "test"
    ] = Field(
        ...,
        description="Helm lifecycle hook type"
    )
    name: str = Field(
        ...,
        description="Name of the hook job",
        pattern=r"^[a-z0-9-]+$"
    )
    purpose: str = Field(
        ...,
        description="What this hook accomplishes (e.g., 'Run database migrations', 'Backup data')",
        min_length=10,
        max_length=200
    )
    weight: Optional[int] = Field(  # Hook execution order
        0,
        description="Hook weight for execution ordering (lower executes first)",
        ge=-100,
        le=100
    )
    delete_policy: Optional[List[Literal["before-hook-creation", "hook-succeeded", "hook-failed"]]] = Field(
        default_factory=lambda: ["before-hook-creation"],
        description="When to delete the hook resource"
    )


class DependenciesOutput(BaseModel):
    """Complete output of dependency checking."""
    
    helm_dependencies: List[HelmDependency] = Field(
        default_factory=list,
        description="List of Helm chart dependencies (subcharts) like postgresql, redis, rabbitmq",
        max_items=20
    )
    init_containers_needed: List[InitContainer] = Field(
        default_factory=list,
        description="List of init containers required for pre-startup tasks",
        max_items=10
    )
    sidecars_needed: List[Sidecar] = Field(
        default_factory=list,
        description="List of sidecar containers that run alongside the main application",
        max_items=10
    )
    helm_hooks: List[HelmHook] = Field(
        default_factory=list,
        description="List of Helm hooks needed for lifecycle management",
        max_items=10
    )
    dependency_rationale: str = Field(
        ...,
        description="Detailed explanation of dependency analysis and selections",
        min_length=50,
        max_length=1000
    )
    warnings: Optional[List[str]] = Field(
        default_factory=list,
        description="Potential concerns or tradeoffs with selected dependencies",
        max_items=5
    )

class ApplicationAnalysisOutput(BaseModel):
    """Complete output of application requirements analysis."""
    
    framework_analysis: FrameworkAnalysis = Field(
        ...,
        description="Detailed framework-specific analysis results"
    )
    scalability: ScalabilityAnalysis = Field(
        ...,
        description="Scalability characteristics and recommendations"
    )
    storage: StorageAnalysis = Field(
        ...,
        description="Storage requirements analysis"
    )
    networking: NetworkingAnalysis = Field(
        ...,
        description="Networking configuration and requirements"
    )
    configuration: ConfigurationAnalysis = Field(
        ...,
        description="Configuration management requirements"
    )
    security: SecurityAnalysis = Field(
        ...,
        description="Security context and requirements"
    )

class KubernetesArchitectureOutput(BaseModel):
    """Complete output of Kubernetes architecture design."""
    
    resources: ResourcesArchitecture = Field(
        ...,
        description="Complete set of Kubernetes resources to be created"
    )
    # design_decisions: List[DesignDecision] = Field(
    #     ...,
    #     description="Structured list of key architectural decisions with rationale",
    #     min_items=3,
    #     max_items=15
    # )

class ResourceEstimationOutput(BaseModel):
    """Complete output of resource estimation across environments."""
    
    dev: EnvironmentResourceSpec = Field(
        ...,
        description="Resource specifications for development environment (minimal resources)"
    )
    staging: EnvironmentResourceSpec = Field(
        ...,
        description="Resource specifications for staging environment (moderate resources)"
    )
    prod: EnvironmentResourceSpec = Field(
        ...,
        description="Resource specifications for production environment (optimal resources)"
    )
    reasoning: str = Field(
        ...,
        description="Detailed explanation of resource estimation rationale including startup time, memory patterns, and scaling considerations",
        min_length=50,
        max_length=1000
    )
    framework_considerations: FrameworkSpecificConsiderations = Field(
        ...,
        description="Framework-specific factors that influenced estimation"
    )
    
    metadata: ResourceEstimationMetadata = Field(
        ...,
        description="Metadata about the estimation process"
    )
    cost_optimization_notes: str = Field(
        ...,
        description="Specific recommendations for cost optimization while maintaining reliability",
        max_length=500
    )

class ResourceMetricName(str, Enum):
    CPU = "cpu"
    MEMORY = "memory"

class MetricTargetType(str, Enum):
    UTILIZATION = "Utilization"  # Percentage
    AVERAGE_VALUE = "AverageValue"  # Absolute value
    VALUE = "Value"  # Total value

class ResourceMetric(BaseModel):
    """Resource-based scaling metric (CPU/Memory)"""
    name: ResourceMetricName
    target_type: MetricTargetType = Field(MetricTargetType.UTILIZATION)
    target_value: int = Field(
        ...,
        ge=1,
        le=100,
        description="Target value (percentage for Utilization, millis for AverageValue)"
    )
    
    @field_validator('target_value')
    @classmethod
    def validate_target_range(cls, v, info: ValidationInfo):
        if info.data.get('target_type') == MetricTargetType.UTILIZATION:
            if v > 100:
                raise ValueError("Utilization target must be <= 100")
        return v


class CustomMetric(BaseModel):
    """Custom pod or external metric"""
    name: str = Field(..., description="Metric name")
    metric_type: Literal["Pods", "Object", "External"] = Field("Pods")
    target_type: MetricTargetType = Field(MetricTargetType.AVERAGE_VALUE)
    target_value: str = Field(..., description="Target value (e.g., '100m', '1k')")
    
    # For Object metrics
    object_kind: Optional[str] = Field(None, description="Object kind (e.g., 'Ingress')")
    object_name: Optional[str] = Field(None, description="Object name")
    
    @field_validator('object_kind')
    @classmethod
    def validate_object_metric_fields(cls, v, info: ValidationInfo):
        if info.data.get('metric_type') == 'Object':
            if not v or not info.data.get('object_name'):
                raise ValueError("Object metrics require object_kind and object_name")
        return v

class HPAGenerationPlanningOutput(BaseModel):
    """
    Output schema for HPA generation planning.
    
    Populated from:
    - planner.scaling_strategy.dev
    - planner.scaling_strategy.staging
    - planner.scaling_strategy.prod
    """
    
    app_name: str = Field(..., description="Application name")
    
    target_kind: Literal["Deployment", "StatefulSet", "ReplicaSet"] = Field(
        "Deployment",
        description="Target workload kind"
    )
    
    target_name: str = Field(
        ...,
        description="Target workload name (will be templated)"
    )
    
    min_replicas: int = Field(
        ...,
        ge=1,
        le=100,
        description="Minimum replicas (from planner.deployment.min_replicas)"
    )
    
    max_replicas: int = Field(
        ...,
        ge=1,
        le=1000,
        description="Maximum replicas (from planner.deployment.max_replicas)"
    )
    
    # Metrics
    resource_metrics: List[ResourceMetric] = Field(
        default=[],
        description="CPU and memory-based metrics"
    )
    
    custom_metrics: List[CustomMetric] = Field(
        default=[],
        description="Custom metrics (pods, object, external)"
    )
    
    # Advanced behavior (K8s 1.18+)
    scaling_behavior: Optional[ScalingBehavior] = Field(
        None,
        description="Advanced scaling behavior configuration"
    )
    
    # Metadata
    labels: Dict[str, str] = Field(default={})
    annotations: Dict[str, str] = Field(default={})
    
    # Planner source
    planner_source_paths: Dict[str, str] = Field(default={})
    
    @field_validator('max_replicas')
    @classmethod
    def validate_max_greater_than_min(cls, v, info: ValidationInfo):
        if 'min_replicas' in info.data and v <= info.data['min_replicas']:
            raise ValueError(
                f"max_replicas ({v}) must be > min_replicas ({info.data['min_replicas']})"
            )
        return v
    
    @field_validator('resource_metrics')
    @classmethod
    def validate_at_least_one_metric(cls, v, info: ValidationInfo):
        if not v and not info.data.get('custom_metrics'):
            raise ValueError("At least one metric (resource or custom) must be specified")
        return v
    
    class Config:
        use_enum_values = True


class ScalingStrategyOutput(BaseModel):
    """Complete output of scaling strategy definition."""
    
    dev: HPAConfiguration = Field(
        ...,
        description="HPA configuration for development environment (minimal scaling)"
    )
    staging: HPAConfiguration = Field(
        ...,
        description="HPA configuration for staging environment (moderate scaling)"
    )
    prod: HPAConfiguration = Field(
        ...,
        description="HPA configuration for production environment (aggressive scaling for HA)"
    )
    scaling_behavior: Optional[ScalingBehavior] = Field(
        None,
        description="Advanced scaling behavior configuration"
    )
    target_kind: Literal["Deployment", "StatefulSet", "ReplicaSet"] = Field(
        "Deployment",
        description="Target workload kind"
    )
    
    selector_labels: Dict[str, str] = Field(
        ...,
        description="Label selector to match pods (must match Deployment labels)"
    )

@tool
async def analyze_application_requirements(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Analyze application requirements and provide detailed technical specifications.
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with application analysis
    """
    try:
        handoff_data = runtime.state.get('handoff_data', {})
        additional_requirements = runtime.state.get('updated_user_requirements', '') or ''
        parsed_requirements = handoff_data.get('parsed_requirements', {})
        parsed_requirements_encoded = json.dumps(parsed_requirements)
        
        # Ensure additional_requirements is a string
        if isinstance(additional_requirements, (dict, list)):
            additional_requirements_encoded = json.dumps(additional_requirements)
        else:
            additional_requirements_encoded = str(additional_requirements)

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = ANALYZE_APPLICATION_REQUIREMENTS_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(parsed_requirements_encoded),
            user_clarification=escape_json_for_template(additional_requirements_encoded)
        )
        parser = PydanticOutputParser(pydantic_object=ApplicationAnalysisOutput)
        escaped_system_prompt = ANALYZE_APPLICATION_REQUIREMENTS_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the ApplicationAnalysisOutput schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        planner_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for application requirements analysis",
            extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        higher_model = LLMProvider.create_llm(
            provider=higher_llm_config['provider'],
            model=higher_llm_config['model'],
            temperature=higher_llm_config['temperature'],
            max_tokens=higher_llm_config['max_tokens']
        )
        chain = prompt | higher_model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Application requirements analysis completed successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,
            "application_analysis": response.model_dump(),
        }
        
        tool_message = ToolMessage(
            content="Application requirements analysis completed successfully. Proceed with design_kubernetes_architecture tool.",
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error analyzing application requirements: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to analyze application requirements: {e}. Please re-run the tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})

@tool
async def design_kubernetes_architecture(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Design a complete Kubernetes architecture for the application.
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with Kubernetes architecture
    """
    try:
        handoff_data = runtime.state.get('handoff_data', {})
        additional_requirements = runtime.state.get('updated_user_requirements', '') or ''
        parsed_requirements = handoff_data.get('parsed_requirements', {})
        application_analysis = handoff_data.get('application_analysis', {})

        parsed_requirements_encoded = encode(parsed_requirements)
        application_analysis_encoded = encode(application_analysis)

        # Ensure additional_requirements is a string
        if isinstance(additional_requirements, (dict, list)):
            additional_requirements_encoded = json.dumps(additional_requirements)
        else:
            additional_requirements_encoded = str(additional_requirements)

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = DESIGN_KUBERNETES_ARCHITECTURE_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(json.dumps(parsed_requirements)),
            analysis=escape_json_for_template(json.dumps(application_analysis)),
            user_clarification=escape_json_for_template(additional_requirements_encoded)
        )
        parser = PydanticOutputParser(pydantic_object=KubernetesArchitectureOutput)
        escaped_system_prompt = DESIGN_KUBERNETES_ARCHITECTURE_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the KubernetesArchitectureOutput schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        planner_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for Kubernetes architecture design",
            extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        higher_model = LLMProvider.create_llm(
            provider=higher_llm_config['provider'],
            model=higher_llm_config['model'],
            temperature=higher_llm_config['temperature'],
            max_tokens=higher_llm_config['max_tokens']
        )
        chain = prompt | higher_model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Kubernetes architecture design completed successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,
            "kubernetes_architecture": response.model_dump(),
        }
        
        tool_message = ToolMessage(
            content="Kubernetes architecture design completed successfully. Proceed with estimate_resources tool.",
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error designing Kubernetes architecture: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to design Kubernetes architecture: {e}. Please re-run the tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})

@tool
async def estimate_resources(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Estimate Kubernetes resource requests and limits for the application across dev, staging, and production environments.
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with resource estimation
    """
    try:
        handoff_data = runtime.state.get('handoff_data', {})
        parsed_requirements = handoff_data.get('parsed_requirements', {})
        application_analysis = handoff_data.get('application_analysis', {})
        
        parsed_requirements_encoded = encode(parsed_requirements)
        application_analysis_encoded = encode(application_analysis)

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = ESTIMATE_RESOURCES_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(parsed_requirements_encoded),
            analysis=escape_json_for_template(application_analysis_encoded)
        )
        parser = PydanticOutputParser(pydantic_object=ResourceEstimationOutput)
        escaped_system_prompt = ESTIMATE_RESOURCES_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the ResourceEstimationOutput schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())
        config = Config()
        llm_config = config.get_llm_config()
        planner_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for resource estimation",
            extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        chain = prompt | model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Resource estimation completed successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,
            "resource_estimation": response.model_dump(),
        }
        
        tool_message = ToolMessage(
            content="Resource estimation completed successfully. Proceed with define_scaling_strategy tool.",
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error estimating resources: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to estimate resources: {e}. Please re-run the tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})

@tool
async def define_scaling_strategy(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Define a scaling strategy for the application across dev, staging, and production environments.
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with scaling strategy
    """
    try:
        handoff_data = runtime.state.get('handoff_data', {})
        parsed_requirements = handoff_data.get('parsed_requirements', {})
        application_analysis = handoff_data.get('application_analysis', {})
        kubernetes_architecture = handoff_data.get('kubernetes_architecture', {})
        
        # Extract PDB and HPA configuration to save tokens
        pdb_config = None
        hpa_config = None
        if kubernetes_architecture:
            auxiliary = kubernetes_architecture.get('resources', {}).get('auxiliary', [])
            for resource in auxiliary:
                res_type = resource.get('type')
                if res_type == 'PodDisruptionBudget':
                    pdb_config = resource
                elif res_type == 'HorizontalPodAutoscaler':
                    hpa_config = resource

        scaling_context = {
            "pdb_config": pdb_config,
            "hpa_config": hpa_config
        }

        parsed_requirements_encoded = json.dumps(parsed_requirements)
        application_analysis_encoded = json.dumps(application_analysis)
        scaling_context_encoded = json.dumps(scaling_context)

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = DEFINE_SCALING_STRATEGY_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(parsed_requirements_encoded),
            analysis=escape_json_for_template(application_analysis_encoded),
            scaling_context=escape_json_for_template(scaling_context_encoded)
        )
        parser = PydanticOutputParser(pydantic_object=ScalingStrategyOutput)
        escaped_system_prompt = DEFINE_SCALING_STRATEGY_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the HPAGenerationPlanningOutput schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        planner_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for scaling strategy definition",
            extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        higher_model = LLMProvider.create_llm(
            provider=higher_llm_config['provider'],
            model=higher_llm_config['model'],
            temperature=higher_llm_config['temperature'],
            max_tokens=higher_llm_config['max_tokens']
        )
        chain = prompt | higher_model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Scaling strategy definition completed successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,
            "scaling_strategy": response.model_dump(),
        }
        
        tool_message = ToolMessage(
            content="Scaling strategy definition completed successfully. Proceed with check_dependencies tool.",
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error defining scaling strategy: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to define scaling strategy: {e}. Please re-run the tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})

@tool
async def check_dependencies(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Check the dependencies for the application.
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with dependencies
    """
    try:
        handoff_data = runtime.state.get('handoff_data', {})
        parsed_requirements = handoff_data.get('parsed_requirements', {})
        parsed_requirements_encoded = encode(parsed_requirements)

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = CHECK_DEPENDENCIES_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(parsed_requirements_encoded)
        )
        parser = PydanticOutputParser(pydantic_object=DependenciesOutput)
        escaped_system_prompt = CHECK_DEPENDENCIES_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the DependenciesOutput schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())
        config = Config()
        llm_config = config.get_llm_config()
        planner_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for dependency checking",
            extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        chain = prompt | model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Dependency checking completed successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,
            "dependencies": response.model_dump(),
        }
        
        tool_message = ToolMessage(
            content="Dependency checking completed successfully. All planning tools have finished and the status is completed.",
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
                "chart_plan": handoff_data,
                "status": "completed"
            },
        )
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error checking dependencies: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to check dependencies: {e}. Please re-run the check_dependencies tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})

@tool
async def generate_ingress_route(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Generate an IngressRoute YAML file for a Helm chart.
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with ingress route
    """
    pass