from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Any, Literal, Optional
from enum import Enum
import json
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .namespace_prompts import NAMESPACE_GENERATOR_SYSTEM_PROMPT, NAMESPACE_GENERATOR_USER_PROMPT

namespace_generator_tool_logger = AgentLogger("NamespaceGeneratorTool")

# ============================================================
# ENUMS
# ============================================================

class NamespaceType(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    SANDBOX = "sandbox"
    SHARED = "shared"
    MONITORING = "monitoring"
    SYSTEM = "system"
    CUSTOM = "custom"

class ResourceQuotaScope(str, Enum):
    MINIMAL = "minimal"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    XLARGE = "xlarge"
    UNLIMITED = "unlimited"
    CUSTOM = "custom"

class PriorityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class NetworkPolicyMode(str, Enum):
    OPEN = "open"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    ISOLATED = "isolated"

# ============================================================
# MODELS
# ============================================================

class ResourceLimits(BaseModel):
    cpu_request: str = "100m"
    cpu_limit: str = "500m"
    memory_request: str = "128Mi"
    memory_limit: str = "512Mi"

class NamespaceQuota(BaseModel):
    total_cpu_requests: str = "1"
    total_cpu_limits: str = "2"
    total_memory_requests: str = "1Gi"
    total_memory_limits: str = "2Gi"
    pod_count: int = 10
    deployment_count: int = 5
    service_count: int = 5
    configmap_count: int = 10
    secret_count: int = 10
    pvc_count: int = 5

class LimitRangeConfig(BaseModel):
    enable_limit_range: bool = True
    pod_default_requests: ResourceLimits = Field(default_factory=ResourceLimits)
    pod_default_limits: ResourceLimits = Field(default_factory=lambda: ResourceLimits(cpu_limit="1", memory_limit="512Mi"))
    min_cpu: str = "10m"
    max_cpu: str = "2"
    min_memory: str = "32Mi"
    max_memory: str = "2Gi"

class NetworkPolicyConfig(BaseModel):
    enable_network_policy: bool = False
    policy_mode: NetworkPolicyMode = NetworkPolicyMode.OPEN
    allow_external_ingress: bool = False
    allow_dns_egress: bool = True


class NamespaceGenerationOutput(BaseModel):
    """Output schema for Namespace YAML generation - simplified to match standard pattern"""
    
    yaml_content: str = Field(..., description="Complete Namespace YAML with all resources")
    file_name: str = Field(default="namespace.yaml")
    template_variables_used: List[str] = Field(default=[])
    helm_template_functions_used: List[str] = Field(default=[])
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = Field(default=[])
    metadata: Dict[str, Any] = Field(default={})
    
    # Namespace-specific fields
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(
        default=["Namespace"],
        description="List of resources generated (e.g., ['Namespace', 'ResourceQuota', 'LimitRange'])"
    )
    namespace_name: str = Field(..., description="The namespace name")

    class Config:
        extra = "forbid"


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def resolve_quota_preset(scope: ResourceQuotaScope, custom_quota: Optional[NamespaceQuota] = None) -> NamespaceQuota:
    if scope == ResourceQuotaScope.CUSTOM and custom_quota:
        return custom_quota
        
    presets = {
        ResourceQuotaScope.MINIMAL: NamespaceQuota(
            total_cpu_requests="500m", total_cpu_limits="1",
            total_memory_requests="512Mi", total_memory_limits="1Gi",
            pod_count=5
        ),
        ResourceQuotaScope.SMALL: NamespaceQuota(
            total_cpu_requests="2", total_cpu_limits="4",
            total_memory_requests="2Gi", total_memory_limits="4Gi",
            pod_count=10
        ),
        ResourceQuotaScope.MEDIUM: NamespaceQuota(
            total_cpu_requests="4", total_cpu_limits="8",
            total_memory_requests="4Gi", total_memory_limits="8Gi",
            pod_count=20
        ),
        ResourceQuotaScope.LARGE: NamespaceQuota(
            total_cpu_requests="8", total_cpu_limits="16",
            total_memory_requests="8Gi", total_memory_limits="16Gi",
            pod_count=50
        ),
        ResourceQuotaScope.XLARGE: NamespaceQuota(
            total_cpu_requests="16", total_cpu_limits="32",
            total_memory_requests="16Gi", total_memory_limits="32Gi",
            pod_count=100
        ),
        ResourceQuotaScope.UNLIMITED: NamespaceQuota(
            total_cpu_requests="1000", total_cpu_limits="1000",
            total_memory_requests="1000Gi", total_memory_limits="1000Gi",
            pod_count=10000
        )
    }
    return presets.get(scope, presets[ResourceQuotaScope.MEDIUM])


# ============================================================
# TOOL DEFINITION
# ============================================================

@tool
async def generate_namespace_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate Kubernetes Namespace YAML including ResourceQuota and LimitRange.
    
    Args:
        runtime: The runtime context
        tool_call_id: The tool call ID
    
    Returns:
        A Command object containing the generated Namespace YAML
    """
    try:
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate Namespace")
            
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        k8s_arch = planner_output.get("kubernetes_architecture", {}) or {}
        resource_est = planner_output.get("resource_estimation", {}) or {}
        
        # Extract inputs
        app_name = parsed_reqs.get("app_name", "myapp")
        core_resources = k8s_arch.get("resources", {}).get("core", {})
        namespace_name = core_resources.get("key_configuration_parameters", {}).get("namespace", "production")
        
        # Determine environment type
        env_type = NamespaceType.PRODUCTION
        if "dev" in namespace_name:
            env_type = NamespaceType.DEVELOPMENT
        elif "staging" in namespace_name:
            env_type = NamespaceType.STAGING
            
        # Determine quota scope based on complexity
        complexity = planner_output.get("complexity_classification", {}).get("complexity_level", "medium")
        quota_scope = ResourceQuotaScope.MEDIUM
        if complexity == "simple":
            quota_scope = ResourceQuotaScope.SMALL
        elif complexity == "complex":
            quota_scope = ResourceQuotaScope.LARGE
            
        # Resolve quota
        resolved_quota = resolve_quota_preset(quota_scope)
        
        # Prepare LimitRange config
        prod_est = resource_est.get("prod", {})
        limit_range_config = LimitRangeConfig(
            enable_limit_range=True,
            pod_default_requests=ResourceLimits(
                cpu_request=prod_est.get("requests", {}).get("cpu", "100m"),
                memory_request=prod_est.get("requests", {}).get("memory", "128Mi")
            ),
            pod_default_limits=ResourceLimits(
                cpu_limit=prod_est.get("limits", {}).get("cpu", "500m"),
                memory_limit=prod_est.get("limits", {}).get("memory", "512Mi")
            )
        )
        
        namespace_generator_tool_logger.log_structured(
            level="INFO",
            message="Generating Namespace YAML",
            extra={
                "tool_call_id": tool_call_id,
                "namespace": namespace_name,
                "type": env_type.value
            }
        )
        
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')
            
        # Extract helper templates from previous tool execution
        tool_results_state = runtime.state.get("tool_results", {})
        helper_output = tool_results_state.get("generate_helpers_tpl", {}).get("output", {})
        template_categories = helper_output.get("template_categories", {})
        
        naming_templates = template_categories.get("naming", [])
        label_templates = template_categories.get("labels", [])
        annotation_templates = template_categories.get("annotations", [])
        
        # Format user query
        formatted_user_query = NAMESPACE_GENERATOR_USER_PROMPT.format(
            namespace_name=namespace_name,
            namespace_type=env_type.value,
            priority_level=PriorityLevel.MEDIUM.value,
            team="devops",
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2),
            enable_resource_quota=True,
            quota_scope=quota_scope.value,
            total_cpu_requests=resolved_quota.total_cpu_requests,
            total_cpu_limits=resolved_quota.total_cpu_limits,
            total_memory_requests=resolved_quota.total_memory_requests,
            total_memory_limits=resolved_quota.total_memory_limits,
            pod_count=resolved_quota.pod_count,
            service_count=resolved_quota.service_count,
            configmap_count=resolved_quota.configmap_count,
            secret_count=resolved_quota.secret_count,
            pvc_count=resolved_quota.pvc_count,
            enable_limit_range=limit_range_config.enable_limit_range,
            default_request_cpu=limit_range_config.pod_default_requests.cpu_request,
            default_request_memory=limit_range_config.pod_default_requests.memory_request,
            default_limit_cpu=limit_range_config.pod_default_limits.cpu_limit,
            default_limit_memory=limit_range_config.pod_default_limits.memory_limit,
            min_cpu=limit_range_config.min_cpu,
            min_memory=limit_range_config.min_memory,
            max_cpu=limit_range_config.max_cpu,
            max_memory=limit_range_config.max_memory,
            enable_network_policy=False,
            policy_mode=NetworkPolicyMode.OPEN.value,
            allow_external_ingress=True,
            allow_dns_egress=True
        )
        
        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        parser = PydanticOutputParser(pydantic_object=NamespaceGenerationOutput)
        
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the NamespaceGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        
        config = Config()
        higher_llm_config = config.get_llm_higher_config()
        
        namespace_generator_tool_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for Namespace generation",
            extra={
                "llm_provider": higher_llm_config.get('provider'),
                "llm_model": higher_llm_config.get('model'),
            }
        )
        
        higher_model = LLMProvider.create_llm(
            provider=higher_llm_config['provider'],
            model=higher_llm_config['model'],
            temperature=higher_llm_config['temperature'],
            max_tokens=higher_llm_config['max_tokens']
        )
        
        chain = prompt | higher_model | parser
        
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=NAMESPACE_GENERATOR_SYSTEM_PROMPT)]
        })
        
        namespace_generator_tool_logger.log_structured(
            level="INFO",
            message="Namespace YAML generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        response_json = response.model_dump()
        namespace_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "namespace.yaml")
        template_variables = response_json.get("template_variables_used", [])
        
        # Prepare tool output
        tool_message = ToolMessage(
            content="Namespace YAML generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_namespace_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_namespace_yaml"]
        else:
            completed_tools = current_completed_tools
            
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_namespace_yaml": {
                "status": "success",
                "output": response_json,
                "validation_messages": response_json.get("validation_messages", [])
            }
        }
        
        # Update generation metadata
        current_metadata = runtime.state.get("generation_metadata", {})
        generation_metadata = current_metadata.copy()
        generation_metadata["tools_executed"] = completed_tools
        if "quality_scores" not in generation_metadata:
            generation_metadata["quality_scores"] = {}
        score = 1.0 if response_json.get("validation_status") == "valid" else 0.8
        generation_metadata["quality_scores"]["generate_namespace_yaml"] = score
        
        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0
        
        return Command(
            update={
                "generated_templates": {
                    file_name: namespace_yaml
                },
                "template_variables": template_variables,
                "messages": [tool_message],
                "completed_tools": completed_tools,
                "tool_results": tool_results,
                "generation_metadata": generation_metadata,
                "coordinator_state": coordinator_state,
                "next_action": "coordinator"
            }
        )
        
    except Exception as e:
        error_context = {
            "error": str(e),
            "error_type": type(e).__name__,
            "tool_call_id": tool_call_id
        }
        
        try:
            if runtime and runtime.state:
                error_context["state_keys"] = list(runtime.state.keys()) if hasattr(runtime.state, 'keys') else "N/A"
                error_context["has_planner_output"] = "planner_output" in runtime.state if runtime.state else False
        except:
            pass
        
        namespace_generator_tool_logger.log_structured(
            level="ERROR",
            message=f"Error generating Namespace YAML: {str(e)}",
            extra=error_context
        )
        raise e
