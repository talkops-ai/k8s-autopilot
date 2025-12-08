from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import ToolMessage, SystemMessage
from typing_extensions import Annotated
from toon import encode
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Any, Optional, Literal
import json
from enum import Enum
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .deployment_prompts import DEPLOYMENT_GENERATOR_SYSTEM_PROMPT, DEPLOYMENT_GENERATOR_USER_PROMPT

deployment_generator_logger = AgentLogger("deployment_generator")

class WorkloadType(str, Enum):
    DEPLOYMENT = "Deployment"
    STATEFULSET = "StatefulSet"

class ResourceRequirements(BaseModel):
    """CPU and memory resource specifications"""
    cpu_request: str = Field(
        ..., 
        description="CPU request (e.g., '500m', '1', '2000m')",
        pattern=r'^\d+m?$'
    )
    memory_request: str = Field(
        ..., 
        description="Memory request (e.g., '1Gi', '512Mi', '2Gi')",
        pattern=r'^\d+(Mi|Gi)$'
    )
    cpu_limit: Optional[str] = Field(
        None, 
        description="CPU limit - should be >= cpu_request",
        pattern=r'^\d+m?$'
    )
    memory_limit: Optional[str] = Field(
        None, 
        description="Memory limit - should be >= memory_request",
        pattern=r'^\d+(Mi|Gi)$'
    )
    
    @field_validator('cpu_limit')
    def validate_cpu_limit(cls, v, values):
        if v and 'cpu_request' in values:
            request = int(values['cpu_request'].rstrip('m'))
            limit = int(v.rstrip('m'))
            if limit < request:
                raise ValueError(f"CPU limit ({v}) must be >= request ({values['cpu_request']})")
        return v

class ProbeConfig(BaseModel):
    """Health check probe configuration"""
    enabled: bool = Field(True, description="Enable this probe")
    probe_type: Literal["httpGet", "tcpSocket", "exec"] = Field("httpGet")
    
    # HTTP-specific fields
    path: Optional[str] = Field("/health", description="HTTP path for httpGet probe")
    port: Optional[int] = Field(8080, ge=1, le=65535, description="Port for probe")
    
    # TCP-specific fields
    tcp_port: Optional[int] = Field(None, ge=1, le=65535)
    
    # Exec-specific fields
    command: Optional[List[str]] = Field(None, description="Command for exec probe")
    
    # Timing parameters
    initial_delay_seconds: int = Field(30, ge=0, description="Delay before first probe")
    period_seconds: int = Field(10, ge=1, description="How often to probe")
    timeout_seconds: int = Field(5, ge=1, description="Probe timeout")
    success_threshold: int = Field(1, ge=1, description="Min consecutive successes")
    failure_threshold: int = Field(3, ge=1, description="Min consecutive failures")
    
    @field_validator('command')
    def validate_command(cls, v, values):
        if values.get('probe_type') == 'exec' and not v:
            raise ValueError("Command is required for exec probe type")
        return v

class SecurityContextConfig(BaseModel):
    """Pod and container security context"""
    # Container security context
    run_as_non_root: bool = Field(True, description="Require non-root user")
    run_as_user: Optional[int] = Field(None, ge=1, description="UID to run as")
    run_as_group: Optional[int] = Field(None, ge=1, description="GID to run as")
    read_only_root_filesystem: bool = Field(False, description="Mount root FS as read-only")
    allow_privilege_escalation: bool = Field(False, description="Allow setuid/setgid")
    
    # Capabilities
    capabilities_drop: List[str] = Field(
        default=["ALL"], 
        description="Capabilities to drop"
    )
    capabilities_add: List[str] = Field(
        default=[], 
        description="Capabilities to add"
    )
    
    # Pod security context
    fsgroup: Optional[int] = Field(None, ge=1, description="FSGroup for volumes")
    fsgroup_change_policy: Optional[Literal["OnRootMismatch", "Always"]] = Field(None)
    
    seccomp_profile: Optional[Dict[str, str]] = Field(
        default={"type": "RuntimeDefault"},
        description="Seccomp profile"
    )

class DeploymentStrategy(BaseModel):
    """Deployment update strategy"""
    type: Literal["RollingUpdate", "Recreate"] = Field("RollingUpdate")
    max_surge: Optional[str] = Field("25%", description="Max surge for RollingUpdate")
    max_unavailable: Optional[str] = Field("25%", description="Max unavailable for RollingUpdate")

class AffinityConfig(BaseModel):
    """Pod affinity and anti-affinity rules"""
    pod_anti_affinity: Optional[Dict[str, Any]] = Field(
        None,
        description="Anti-affinity to spread pods across nodes/zones"
    )
    node_affinity: Optional[Dict[str, Any]] = Field(
        None,
        description="Affinity to specific node types/labels"
    )

class VolumeMount(BaseModel):
    """Volume mount specification"""
    name: str = Field(..., description="Volume name")
    mount_path: str = Field(..., description="Mount path in container")
    read_only: bool = Field(False)
    sub_path: Optional[str] = Field(None)

class EnvironmentVariable(BaseModel):
    """Environment variable definition"""
    name: str = Field(..., description="Env var name")
    value: Optional[str] = Field(None, description="Direct value")
    value_from: Optional[Dict[str, Any]] = Field(
        None,
        description="Reference to ConfigMap, Secret, or field"
    )
    @field_validator('value_from')
    def validate_value_or_value_from(cls, v, values):
        if v is None and 'value' not in values:
            raise ValueError("Either value or value_from must be specified")
        if v is not None and 'value' in values and values['value'] is not None:
            raise ValueError("Cannot specify both value and value_from")
        return v


class DeploymentGenerationInput(BaseModel):
    """
    Input schema for Deployment/StatefulSet YAML generation.
    
    This schema is populated from planner output (parse_req_4_mini.json).
    """
    
    # ============================================================
    # REQUIRED FIELDS
    # ============================================================
    
    app_name: str = Field(
        ..., 
        description="Application name (from planner.parsed_requirements.app_name)",
        min_length=1,
        max_length=63,
        pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
    )
    
    workload_type: WorkloadType = Field(
        ..., 
        description="Deployment or StatefulSet (from planner.kubernetes_architecture.resources.core.type)"
    )
    
    image: str = Field(
        ..., 
        description="Container image with tag (from planner.parsed_requirements.image.full_image)",
        pattern=r'^[a-z0-9\.\-\/]+:[a-z0-9\.\-\_]+$'
    )
    
    replicas: int = Field(
        2, 
        ge=1, 
        le=100,
        description="Number of replicas (from planner.parsed_requirements.deployment.min_replicas)"
    )
    
    resources: ResourceRequirements = Field(
        ...,
        description="Resource requests and limits (from planner.resource_estimation.prod)"
    )
    
    # ============================================================
    # OPTIONAL FIELDS WITH DEFAULTS
    # ============================================================
    
    namespace: str = Field(
        "default",
        description="Target namespace"
    )
    
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = Field(
        "IfNotPresent",
        description="Image pull policy - set to Always for :latest tag"
    )
    
    image_pull_secrets: List[str] = Field(
        default=[],
        description="Secret names for private registry authentication"
    )
    
    service_account_name: Optional[str] = Field(
        None,
        description="Service account name for pod"
    )
    
    # Probes
    liveness_probe: Optional[ProbeConfig] = Field(
        None,
        description="Liveness probe configuration (from planner.application_analysis.framework_analysis)"
    )
    
    readiness_probe: Optional[ProbeConfig] = Field(
        None,
        description="Readiness probe configuration (from planner.application_analysis.framework_analysis)"
    )
    
    startup_probe: Optional[ProbeConfig] = Field(
        None,
        description="Startup probe for slow-starting containers"
    )
    
    # Security
    security_context: SecurityContextConfig = Field(
        default_factory=SecurityContextConfig,
        description="Security context configuration (from planner.application_analysis.security)"
    )
    
    # Environment and volumes
    environment_variables: List[EnvironmentVariable] = Field(
        default=[],
        description="Environment variables (from planner.parsed_requirements.configuration)"
    )
    
    volume_mounts: List[VolumeMount] = Field(
        default=[],
        description="Volume mounts"
    )
    
    volumes: List[Dict[str, Any]] = Field(
        default=[],
        description="Volume definitions (ConfigMap, Secret, PVC, etc.)"
    )
    
    # Strategy and affinity
    strategy: Optional[DeploymentStrategy] = Field(
        default_factory=lambda: DeploymentStrategy(type="RollingUpdate"),
        description="Update strategy"
    )
    
    affinity: Optional[AffinityConfig] = Field(
        None,
        description="Pod affinity rules for HA deployments"
    )
    
    # Labels and annotations
    labels: Dict[str, str] = Field(
        default={},
        description="Additional labels beyond standard ones"
    )
    
    annotations: Dict[str, str] = Field(
        default={},
        description="Pod annotations"
    )
    
    # Advanced features
    termination_grace_period_seconds: int = Field(
        30,
        ge=0,
        description="Grace period for pod termination"
    )
    
    dns_policy: Literal["ClusterFirst", "ClusterFirstWithHostNet", "Default", "None"] = Field(
        "ClusterFirst"
    )
    
    restart_policy: Literal["Always", "OnFailure", "Never"] = Field("Always")
    
    # StatefulSet-specific
    service_name: Optional[str] = Field(
        None,
        description="Headless service name for StatefulSet"
    )
    
    volume_claim_templates: List[Dict[str, Any]] = Field(
        default=[],
        description="PVC templates for StatefulSet"
    )
    
    pod_management_policy: Optional[Literal["OrderedReady", "Parallel"]] = Field(
        None,
        description="StatefulSet pod management policy"
    )
    
    # ============================================================
    # PLANNER SOURCE MAPPING (for traceability)
    # ============================================================
    
    planner_source_paths: Dict[str, str] = Field(
        default={},
        description="Maps each field to its source path in planner JSON"
    )
    
    # ============================================================
    # VALIDATORS
    # ============================================================
    
    @field_validator('image_pull_policy')
    def set_pull_policy_for_latest_tag(cls, v, values):
        """Set Always for :latest tags"""
        if 'image' in values and values['image'].endswith(':latest'):
            return "Always"
        return v
    
    @field_validator('service_name')
    def require_service_name_for_statefulset(cls, v, values):
        """StatefulSet requires service_name"""
        if values.get('workload_type') == WorkloadType.STATEFULSET and not v:
            raise ValueError("StatefulSet requires service_name for headless service")
        return v
    
    @field_validator('volume_claim_templates')
    def validate_statefulset_pvcs(cls, v, values):
        """Validate PVC templates for StatefulSet"""
        if values.get('workload_type') == WorkloadType.STATEFULSET:
            if not v:
                raise ValueError("StatefulSet requires at least one volumeClaimTemplate for persistent storage")
        return v
    
    class Config:
        use_enum_values = True
        extra = "forbid"  # Reject unknown fields


class DeploymentGenerationOutput(BaseModel):
    """
    Output schema for Deployment/StatefulSet YAML generation.
    
    This ensures the tool returns structured, validated output.
    """
    
    yaml_content: str = Field(
        ..., 
        description="Complete Deployment or StatefulSet YAML with Helm templating",
        min_length=100  # Ensure non-trivial output
    )
    
    file_name: str = Field(
        ..., 
        description="Filename (deployment.yaml or statefulset.yaml)",
        pattern=r'^(deployment|statefulset)\.yaml$'
    )
    
    template_variables_used: List[str] = Field(
        ..., 
        description="All {{ .Values.* }} references found in the YAML",
        min_items=5  # Ensure templating was applied
    )
    
    helm_template_functions_used: List[str] = Field(
        default=[],
        description="Helper functions referenced (e.g., 'include \"app.fullname\" .')"
    )
    
    validation_status: Literal["valid", "warning", "error"] = Field(
        ..., 
        description="Pre-validation result before returning"
    )
    
    validation_messages: List[str] = Field(
        default=[],
        description="Validation warnings or errors"
    )
    
    metadata: Dict[str, Any] = Field(
        default={},
        description="Generation metadata (model, tokens, time)"
    )
    
    kubernetes_api_version: str = Field(
        default="apps/v1",
        description="Kubernetes API version used"
    )
    
    generated_resources: List[str] = Field(
        default=[],
        description="List of Kubernetes resources in the YAML (usually 1 for Deployment)"
    )
    
    security_features_applied: List[str] = Field(
        default=[],
        description="Security features implemented (e.g., 'runAsNonRoot', 'readOnlyRootFilesystem')"
    )
    
    @field_validator('yaml_content')
    @classmethod
    def validate_yaml_syntax(cls, v):
        """
        Validate YAML structure by stripping Helm template directives.
        Helm templates contain {{ ... }} syntax which is not valid YAML until rendered.
        We strip template syntax and validate the underlying YAML structure.
        """
        import re
        import yaml
        
        # Check if content contains Helm template syntax
        has_helm_syntax = bool(re.search(r'\{\{', v))
        
        if not has_helm_syntax:
            # No Helm syntax - validate as pure YAML
            try:
                parsed = yaml.safe_load(v)
                if parsed and parsed.get('kind') not in ['Deployment', 'StatefulSet']:
                    raise ValueError("Invalid resource kind (expected Deployment or StatefulSet)")
            except Exception as e:
                raise ValueError(f"Invalid YAML syntax: {str(e)}")
            return v
        
        # Has Helm template syntax - strip it for structure validation
        stripped_yaml = v
        # Replace all {{ ... }} blocks (including multi-line) with placeholders
        # This regex handles {{- ... }}, {{ ... }}, and multi-line templates
        stripped_yaml = re.sub(r'\{\{-?\s*[^}]*\}\}', 'PLACEHOLDER', stripped_yaml, flags=re.DOTALL)
        
        try:
            parsed = yaml.safe_load(stripped_yaml)
            if parsed is None:
                # Only comments/whitespace after stripping - might be valid Helm template
                return v
            # Validate that it's a Deployment/StatefulSet resource structure (even if values are placeholders)
            if parsed.get('kind') not in ['Deployment', 'StatefulSet']:
                raise ValueError("Generated YAML structure is not a valid resource kind (expected Deployment or StatefulSet)")
        except Exception as e:
            # For Helm templates, we're more lenient - structure validation is best effort
            # The actual validation happens when Helm renders the template
            # Log warning but don't fail - LLM should generate correct structure
            deployment_generator_logger.log_structured(
                level="WARNING",
                message="YAML structure validation warning for Helm template",
                extra={"error": str(e), "note": "Helm templates may not parse as pure YAML"}
            )
            # Don't raise - allow Helm template syntax through
        
        return v
    
    # Removed validate_yaml_syntax because Helm templates are not valid YAML
    # due to {{ ... }} syntax. We rely on the LLM to generate correct structure.
    
    class Config:
        extra = "forbid"




@tool
async def generate_deployment_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
    ) -> Command:
    """
    Generate a Deployment YAML file for a Helm chart.
    
    Args:
        runtime: The runtime object for the tool.
        tool_call_id: The ID of the tool call.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate deployment YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        app_analysis = planner_output.get("application_analysis", {}) or {}
        k8s_arch = planner_output.get("kubernetes_architecture", {}) or {}
        resource_arch = planner_output.get("resource_estimation", {}) or {}
        
        # Determine workload type - core is now a list
        resources = k8s_arch.get("resources", {}) or {}
        core_resources_list = resources.get("core", []) or []
        
        # Find Deployment or StatefulSet resource from core list
        deployment_resource = None
        for resource in core_resources_list:
            res_type = resource.get("type")
            if res_type in ["Deployment", "StatefulSet"]:
                deployment_resource = resource
                break
        
        workload_type = deployment_resource.get("type") if deployment_resource else "Deployment"
    
        # Extract security settings from analysis (runtime security context)
        security_analysis = app_analysis.get("security", {}) or {}
        # Extract security requirements (policies)
        security_reqs = parsed_reqs.get("security", {}) or {}
    
        # Extract framework-specific probe settings
        framework_analysis = app_analysis.get("framework_analysis", {}) or {}
        networking = app_analysis.get("networking", {}) or {}
        configuration = app_analysis.get("configuration", {}) or {}
    
        # Extract resource requirements - provide default empty dict if None
        prod_resources = resource_arch.get("prod") or {}

        # Extract application details 
        app_name = parsed_reqs.get("app_name", "myapp")
        chart_name = app_name # Default chart_name to app_name
        
        app_type = parsed_reqs.get("app_type" , "")
        framework = parsed_reqs.get("framework", "")
        language = parsed_reqs.get("language", "")

        # Extract Container Image
        image_info = parsed_reqs.get("image", {}) or {}
        full_image = image_info.get("full_image", "")
        repository = image_info.get("repository", "")
        tag = image_info.get("tag", "")
        
        # Extract Deployment Configuration
        deployment_config = parsed_reqs.get("deployment", {}) or {}
        min_replicas = deployment_config.get("min_replicas", 1)
        max_replicas = deployment_config.get("max_replicas", 3)
        high_availability = deployment_config.get("high_availability", False)
        region = deployment_config.get("regions", [])
        
        # Extract resource Requirements - use prod_resources or default empty dict
        resource_req = prod_resources if prod_resources else {}

        # Extract Security Settings
        pod_security_policy = security_reqs.get("pod_security_policy", "")
        network_policy = security_reqs.get("network_policies", False)
        rbac_required = security_reqs.get("rbac_required", False)
        tls_encryption = security_reqs.get("tls_encryption", False)
        

        # Extract framework-specific probe settings
        framework = parsed_reqs.get("framework", "")

        # Extract environment variables
        config_section = parsed_reqs.get("configuration", {}) or {}
        env_vars = config_section.get("environment_variables", [])
        
        # Extract core deployment configuration from k8s_architecture - use the deployment_resource we found earlier
        core_config = deployment_resource.get("key_configuration_parameters", {}) if deployment_resource else {}

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')
        
        # Extract helper templates from previous tool execution
        tool_results = runtime.state.get("tool_results", {})
        helper_output = tool_results.get("generate_helpers_tpl", {}).get("output", {})
        template_categories = helper_output.get("template_categories", {})
        
        naming_templates = template_categories.get("naming", [])
        label_templates = template_categories.get("labels", [])
        annotation_templates = template_categories.get("annotations", [])

        formatted_user_query = DEPLOYMENT_GENERATOR_USER_PROMPT.format(
            workload_type=workload_type,
            app_name=app_name,
            app_type=app_type,
            language=language,
            framework=framework,
            framework_analysis=escape_json_for_template(json.dumps(framework_analysis)),
            full_image=full_image,
            repository=repository,
            tag=tag,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            high_availability=high_availability,
            regions=region,
            resource_req=escape_json_for_template(json.dumps(resource_req)),
            pod_security_policy=pod_security_policy,
            network_policy=network_policy,
            rbac_required=rbac_required,
            tls_encryption=tls_encryption,
            env_vars=env_vars,
            networking=escape_json_for_template(json.dumps(networking)),
            configuration=escape_json_for_template(json.dumps(configuration)),
            security=escape_json_for_template(json.dumps(security_analysis)),
            core_config=escape_json_for_template(json.dumps(core_config, indent=2)),
            chart_name=chart_name,
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2)
        )
        parser = PydanticOutputParser(return_id=True, pydantic_object=DeploymentGenerationOutput)
        
        # Use MessagesPlaceholder for the system prompt to completely bypass template parsing
        # This prevents LangChain from interpreting {{ .Values.* }} as variables
        
        # We also need to escape the user query because it contains Helm syntax like { .Values.* }
        # which ChatPromptTemplate will try to parse as variables
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the DeploymentGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        
        deployment_generator_logger.log_structured(
            level="INFO",
            message="Generating Deployment YAML file for Helm chart",
            extra={
                "chart_name": chart_name,
                "app_name": app_name,
            }
        )
        
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        
        chain = prompt | model | parser
        
        try:
            # Pass the system message directly in the input
            response = await chain.ainvoke({
                "system_message": [SystemMessage(content=DEPLOYMENT_GENERATOR_SYSTEM_PROMPT)]
            })
            
            deployment_generator_logger.log_structured(
                level="INFO",
                message="Deployment YAML file generated successfully",
                extra={
                    "response": response.model_dump(),
                    "tool_call_id": tool_call_id
                }
            )
            response_json = response.model_dump()
            deployment_yaml = response_json.get("yaml_content", "")
            file_name = response_json.get("file_name", "")
            template_variable = response_json.get("template_variables_used", [])
    
            tool_message = ToolMessage(
                content="Deployment YAML file generated successfully.",
                tool_call_id=tool_call_id
            )
            
            # Update state tracking
            current_completed_tools = runtime.state.get("completed_tools", [])
            if "generate_deployment_yaml" not in current_completed_tools:
                completed_tools = current_completed_tools + ["generate_deployment_yaml"]
            else:
                completed_tools = current_completed_tools

            # Update tool results
            current_tool_results = runtime.state.get("tool_results", {})
            tool_results = {
                **current_tool_results,
                "generate_deployment_yaml": {
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
            # Simple quality score based on validation status
            score = 1.0 if response_json.get("validation_status") == "valid" else 0.8
            generation_metadata["quality_scores"]["generate_deployment_yaml"] = score

            # Reset retry count in coordinator state
            current_coordinator_state = runtime.state.get("coordinator_state", {})
            coordinator_state = current_coordinator_state.copy()
            coordinator_state["current_retry_count"] = 0

            return Command(
                update={
                    "messages": [tool_message],
                    "generated_templates": {
                        file_name: deployment_yaml
                    },
                    "template_variables": template_variable,
                    # State tracking updates
                    "completed_tools": completed_tools,
                    "tool_results": tool_results,
                    "generation_metadata": generation_metadata,
                    "coordinator_state": coordinator_state,
                    "next_action": "coordinator"
                },
            )
            
        except Exception as e:
            deployment_generator_logger.log_structured(
                level="ERROR",
                message=f"Error generating Deployment YAML file: {str(e)}",
                extra={
                    "error": str(e),
                    "tool_call_id": tool_call_id
                }
            )
            raise e

    except Exception as e:
        # Log detailed error information for debugging
        error_context = {
            "error": str(e),
            "error_type": type(e).__name__,
            "tool_call_id": tool_call_id
        }
        
        # Try to log state information if available (without causing another error)
        try:
            if runtime and runtime.state:
                error_context["state_keys"] = list(runtime.state.keys()) if hasattr(runtime.state, 'keys') else "N/A"
                error_context["has_planner_output"] = "planner_output" in runtime.state if runtime.state else False
        except:
            pass  # Don't fail on logging
        
        deployment_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating Deployment YAML file: {e}",
            extra=error_context
        )
        # Re-raise the exception so the Coordinator can handle retries and error state updates
        raise e
        


