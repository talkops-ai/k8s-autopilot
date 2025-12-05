from pydantic import BaseModel, Field, field_validator
from typing import Dict, Optional, Literal, List, Any
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from k8s_autopilot.core.state.base import GenerationSwarmState
from typing_extensions import Annotated
from toon import encode
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .helper_prompts import (
    HELPERS_GENERATOR_SYSTEM_PROMPT,
    HELPERS_GENERATOR_USER_PROMPT,
)

# Create agent logger for helper tool
helper_generator_logger = AgentLogger("HelperGenerator")

class TemplateCategories(BaseModel):
    naming: List[str] = Field(default=[], description="Naming templates")
    labels: List[str] = Field(default=[], description="Label templates")
    annotations: List[str] = Field(default=[], description="Annotation templates")
    security: List[str] = Field(default=[], description="Security templates")
    observability: List[str] = Field(default=[], description="Observability templates")
    resources: List[str] = Field(default=[], description="Resource templates")
    rbac: List[str] = Field(default=[], description="RBAC templates")

class HelpersTplGenerationOutput(BaseModel):
    tpl_content: str = Field(..., description="Content of _helpers.tpl")
    file_name: str = Field(default="_helpers.tpl")
    defined_templates: List[str] = Field(..., description="List of defined template names")
    template_categories: TemplateCategories = Field(default_factory=TemplateCategories, description="Categorized templates")
    template_variables_used: List[str] = Field(default=[], description="List of template variables used")
    validation_messages: List[str] = Field(default=[])
    
    @field_validator('defined_templates')
    @classmethod
    def validate_defined_templates(cls, v):
        """Ensure standard templates are defined"""
        # We can check for standard suffixes like .fullname, .labels, etc.
        required_suffixes = ['.fullname', '.labels', '.selectorLabels']
        # This is a loose check because the prefix (CHARTNAME) varies
        found_suffixes = [t.split('.')[-1] for t in v if '.' in t]
        for req in [s.lstrip('.') for s in required_suffixes]:
            if req not in found_suffixes:
                # Warning only, as names might vary slightly
                pass 
        return v
    
    class Config:
        extra = "forbid"


@tool
async def generate_helpers_tpl(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
    ) -> Command:
    """
    Generate a standard _helpers.tpl file for a Helm chart.
    
    Args:
        runtime: The runtime object for the tool.
        tool_call_id: The ID of the tool call.
    """
    try:
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        app_analysis = planner_output.get("application_analysis", {}) or {}
        k8s_arch = planner_output.get("kubernetes_architecture", {}) or {}
        
        # --- Extract Metadata ---
        app_name = parsed_reqs.get("app_name", "")
        chart_name = parsed_reqs.get("chart_name", app_name)
        chart_version = parsed_reqs.get("chart_version", "0.1.0")
        
        # Application Profile
        app_type = parsed_reqs.get("app_type", "api_service")
        
        # Derive Tier
        deployment_conf = parsed_reqs.get("deployment", {}) or {}
        regions = deployment_conf.get("regions", []) or []
        ha = deployment_conf.get("high_availability", False)
        min_replicas = deployment_conf.get("min_replicas", 1)
        
        derived_tier = "dev"
        if ha or len(regions) > 1 or min_replicas > 2:
            derived_tier = "prod-ha"
        elif min_replicas > 1:
            derived_tier = "staging"
            
        # Criticality
        criticality_level = "medium"
        resources = k8s_arch.get("resources", {}) or {}
        aux_resources = resources.get("auxiliary", []) or []
        for res in aux_resources:
            if (res or {}).get("criticality") == "production-critical":
                criticality_level = "high"
                break
        
        owner = "platform-team" # Default as not in input
        
        # Deployment Config
        max_replicas = deployment_conf.get("max_replicas", 1)
        canary_deployment = deployment_conf.get("canary_deployment", False)
        
        # Image Config
        image_conf = parsed_reqs.get("image", {}) or {}
        repository = image_conf.get("repository", "")
        tag = image_conf.get("tag", "latest")
        
        # Try to find pull policy in core resources
        pull_policy = "IfNotPresent"
        core_res = resources.get("core", {}) or {}
        try:
            key_config = core_res.get("key_configuration_parameters", {}) or {}
            pod_spec = key_config.get("podSpec", {}) or {}
            containers = pod_spec.get("containers", []) or []
            if containers:
                pull_policy = containers[0].get("imagePullPolicy", "IfNotPresent")
        except:
            pass
            
        # Service Config
        service_conf = parsed_reqs.get("service", {}) or {}
        access_type = service_conf.get("access_type", "ClusterIP")
        target_port = service_conf.get("target_port")
        if not target_port:
            networking = app_analysis.get("networking", {}) or {}
            target_port = networking.get("port", 8080)
             
        networking = app_analysis.get("networking", {}) or {}
        protocol = networking.get("protocol", "TCP")
        
        ingress_class = "nginx" # Default
        for res in aux_resources:
            if (res or {}).get("type") == "Ingress":
                hints = (res or {}).get("configuration_hints", {}) or {}
                traefik_hints = hints.get("traefik", {}) or {}
                if traefik_hints.get("ingressClassName"):
                    ingress_class = traefik_hints.get("ingressClassName")
                break
                
        # Security Requirements
        security_reqs = parsed_reqs.get("security", {}) or {}
        network_policy_required = security_reqs.get("network_policies", False)
        rbac_required = security_reqs.get("rbac_required", False)
        psp_requirement = security_reqs.get("pod_security_policy", "baseline")
        
        service_mesh_type = "none"
        dependencies = planner_output.get("dependencies", {}) or {}
        # Check sidecars or other hints for service mesh
        
        # Observability
        scalability = app_analysis.get("scalability", {}) or {}
        monitoring_enabled = scalability.get("hpa_enabled", False)
        tracing_enabled = False # Default
        logging_strategy = "standard"
        
        metrics_port = 9090
        sidecars = dependencies.get("sidecars_needed", []) or []
        for sidecar in sidecars:
            sidecar_dict = sidecar or {}
            if "metrics" in sidecar_dict.get("name", ""):
                monitoring_enabled = True
            if "logging" in sidecar_dict.get("name", ""):
                logging_strategy = "sidecar"
        
        # Storage
        storage_analysis = app_analysis.get("storage", {}) or {}
        persistence_required = storage_analysis.get("persistent_storage", False)
        storage_type = "standard" # Default
        backup_required = False
        helm_hooks = dependencies.get("helm_hooks", []) or []
        for hook in helm_hooks:
            hook_dict = hook or {}
            if "backup" in hook_dict.get("name", ""):
                backup_required = True
                
        # Resource Tier
        resource_estimation = planner_output.get("resource_estimation", {}) or {}
        resource_tier_data = resource_estimation.get("prod", {}) or {}
        resource_tier = f"Requests: {resource_tier_data.get('requests', {})}, Limits: {resource_tier_data.get('limits', {})}"

        formatted_user_query = HELPERS_GENERATOR_USER_PROMPT.format(
            chart_name=chart_name,
            chart_version=chart_version,
            app_name=app_name,
            app_type=app_type,
            derived_tier=derived_tier,
            criticality_level=criticality_level,
            owner=owner,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            high_availability=ha,
            canary_deployment=canary_deployment,
            regions=regions,
            repository=repository,
            tag=tag,
            pull_policy=pull_policy,
            access_type=access_type,
            protocol=protocol,
            target_port=target_port,
            ingress_class=ingress_class,
            network_policy_required=network_policy_required,
            rbac_required=rbac_required,
            psp_requirement=psp_requirement,
            service_mesh_type=service_mesh_type,
            monitoring_enabled=monitoring_enabled,
            tracing_enabled=tracing_enabled,
            logging_strategy=logging_strategy,
            metrics_port=metrics_port,
            persistence_required=persistence_required,
            storage_type=storage_type,
            backup_required=backup_required,
            resource_tier=resource_tier
        )
        
        parser = PydanticOutputParser(return_id=True, pydantic_object=HelpersTplGenerationOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the HelpersTplGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        helper_generator_logger.log_structured(
            level="INFO",
            message="Generating _helpers.tpl file for Helm chart",
            extra={
                "app_name": app_name,
                "chart_name": chart_name,
                "tool_call_id": tool_call_id
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
        
        # Create the chain
        chain = prompt | higher_model | parser
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=HELPERS_GENERATOR_SYSTEM_PROMPT)]
        })
        helper_generator_logger.log_structured(
            level="INFO",
            message="_helpers.tpl file generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        tpl_content = response_json.get("tpl_content", "")
        file_name = response_json.get("file_name", "")

        tool_message = ToolMessage(
            content="_helpers.tpl file generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_helpers_tpl" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_helpers_tpl"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_helpers_tpl": {
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
        # Helpers are critical, so if it parses, it's likely good
        score = 1.0 
        generation_metadata["quality_scores"]["generate_helpers_tpl"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "generated_templates": {
                    file_name: tpl_content
                },
                "messages": [tool_message],
                # State tracking updates
                "completed_tools": completed_tools,
                "tool_results": tool_results,
                "generation_metadata": generation_metadata,
                "coordinator_state": coordinator_state,
                "next_action": "coordinator"
            },
        )

    except Exception as e:
        helper_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating _helpers.tpl file: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        # Re-raise exception for coordinator error handling
        raise e