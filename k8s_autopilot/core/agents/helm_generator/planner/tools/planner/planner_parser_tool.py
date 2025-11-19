from pydantic import BaseModel, Field
from typing import Dict, Optional, Literal, List
from langchain.tools import tool, InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from k8s_autopilot.core.state.base import PlanningSwarmState
from typing_extensions import Annotated
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .planner_parser_prompts import (
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
    
    type: Literal["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"] = Field(
        ...,
        description="Type of core Kubernetes workload resource"
    )
    reasoning: str = Field(
        ...,
        description="Technical justification for choosing this resource type",
        min_length=10,
        max_length=500
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
    why_needed: str = Field(
        ...,
        description="Explanation of why this resource is necessary for the application",
        min_length=10,
        max_length=300
    )


class ResourcesArchitecture(BaseModel):
    """Complete Kubernetes resources architecture."""
    
    core: CoreResource = Field(
        ...,
        description="Primary workload resource (Deployment, StatefulSet, etc.)"
    )
    auxiliary: List[AuxiliaryResource] = Field(
        ...,
        description="List of auxiliary resources needed to support the application",
        min_items=1,
        max_items=15
    )

class ResourceSpec(BaseModel):
    """Resource requests and limits specification."""
    
    cpu: str = Field(
        ...,
        description="CPU resource specification (e.g., '100m', '1', '2000m')",
        pattern=r"^\d+m?$"
    )
    memory: str = Field(
        ...,
        description="Memory resource specification (e.g., '256Mi', '1Gi', '512Mi')",
        pattern=r"^\d+(Mi|Gi)$"
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
    reason: str = Field(
        ...,
        description="Why this dependency is needed for the application",
        min_length=10,
        max_length=300
    )


class InitContainer(BaseModel):
    """Init container definition."""
    
    name: str = Field(
        ...,
        description="Name of the init container",
        pattern=r"^[a-z0-9-]+$"
    )
    purpose: str = Field(
        ...,
        description="What this init container does (e.g., database migration, wait-for-service)",
        min_length=10,
        max_length=200
    )


class Sidecar(BaseModel):
    """Sidecar container definition."""
    
    name: str = Field(
        ...,
        description="Name of the sidecar container",
        pattern=r"^[a-z0-9-]+$"
    )
    purpose: str = Field(
        ...,
        description="What this sidecar does (e.g., log shipping, service mesh proxy, metrics collection)",
        min_length=10,
        max_length=200
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
    helm_hooks: List[Literal[
        "pre-install",
        "post-install",
        "pre-delete",
        "post-delete",
        "pre-upgrade",
        "post-upgrade",
        "pre-rollback",
        "post-rollback",
        "test"
    ]] = Field(
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


class KubernetesArchitectureOutput(BaseModel):
    """Complete output of Kubernetes architecture design."""
    
    resources: ResourcesArchitecture = Field(
        ...,
        description="Complete set of Kubernetes resources to be created"
    )
    design_decisions: List[str] = Field(
        ...,
        description="List of key architectural decisions and their rationale",
        min_items=3,
        max_items=20
    )


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
    scaling_rationale: str = Field(
        ...,
        description="Detailed explanation of scaling strategy decisions including min/max replica choices and threshold selections",
        min_length=50,
        max_length=1000
    )


@tool
async def analyze_application_requirements(
    requirements: Dict,
    state: Annotated[PlanningSwarmState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> ApplicationAnalysisOutput:
    """
    Analyze application requirements and provide detailed technical specifications.
    Args:
        requirements: Dictionary containing parsed application requirements
        state: Injected state from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        ApplicationAnalysisOutput: Structured output from application requirements analysis
    """
    try:
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = ANALYZE_APPLICATION_REQUIREMENTS_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(requirements)
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
        chain = prompt | model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Application requirements analysis completed successfully",
            extra={
                "response": response.content.strip(),
                "tool_call_id": tool_call_id
            }
        )
        return response
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error analyzing application requirements: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        return None

@tool
async def design_kubernetes_architecture(
    requirements: Dict,
    analysis: Dict,
    state: Annotated[PlanningSwarmState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> KubernetesArchitectureOutput:
    """
    Design a complete Kubernetes architecture for the application.
    Args:
        requirements: Dictionary containing parsed application requirements
        analysis: Dictionary containing technical analysis of the application
        state: Injected state from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        KubernetesArchitectureOutput: Structured output from Kubernetes architecture design
    """
    try:
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = DESIGN_KUBERNETES_ARCHITECTURE_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(requirements),
            analysis=escape_json_for_template(analysis)
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
        chain = prompt | model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Kubernetes architecture design completed successfully",
            extra={
                "response": response.content.strip(),
                "tool_call_id": tool_call_id
            }
        )
        return response    
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error designing Kubernetes architecture: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        return None


@tool
async def estimate_resources(
    requirements: Dict,
    analysis: Dict,
    state: Annotated[PlanningSwarmState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> ResourceEstimationOutput:
    """
    Estimate Kubernetes resource requests and limits for the application across dev, staging, and production environments.
    Args:
        requirements: Dictionary containing parsed application requirements
        analysis: Dictionary containing technical analysis of the application
        state: Injected state from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        ResourceEstimationOutput: Structured output from resource estimation
    """
    try:
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = ESTIMATE_RESOURCES_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(requirements),
            analysis=escape_json_for_template(analysis)
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
                "response": response.content.strip(),
                "tool_call_id": tool_call_id
            }
        )
        return response
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error estimating resources: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        return None

@tool
async def define_scaling_strategy(
    requirements: Dict,
    analysis: Dict,
    state: Annotated[PlanningSwarmState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> ScalingStrategyOutput:
    """
    Define a scaling strategy for the application across dev, staging, and production environments.
    Args:
        requirements: Dictionary containing parsed application requirements
        analysis: Dictionary containing technical analysis of the application
        state: Injected state from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        ScalingStrategyOutput: Structured output from scaling strategy definition
    """
    try:
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = DEFINE_SCALING_STRATEGY_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(requirements),
        )
        parser = PydanticOutputParser(pydantic_object=ScalingStrategyOutput)
        escaped_system_prompt = DEFINE_SCALING_STRATEGY_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the ScalingStrategyOutput schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())
        config = Config()
        llm_config = config.get_llm_config()
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
        chain = prompt | model | parser
        response = chain.invoke({})
        planner_parser_logger.log_structured(
            level="INFO",
            message="Scaling strategy definition completed successfully",
            extra={
                "response": response.content.strip(),
                "tool_call_id": tool_call_id
            }
        )
        return response
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error defining scaling strategy: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        return None

@tool
async def check_dependencies(
    requirements: Dict,
    state: Annotated[PlanningSwarmState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> DependenciesOutput:
    """
    Check the dependencies for the application.
    Args:
        requirements: Dictionary containing parsed application requirements
        state: Injected state from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        DependenciesOutput: Structured output from dependency checking
    """
    try:
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = CHECK_DEPENDENCIES_HUMAN_PROMPT.format(
            requirements=escape_json_for_template(requirements)
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
                "response": response.content.strip(),
                "tool_call_id": tool_call_id
            }
        )
        return response
    except Exception as e:
        planner_parser_logger.log_structured(
            level="ERROR",
            message=f"Error checking dependencies: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        return None