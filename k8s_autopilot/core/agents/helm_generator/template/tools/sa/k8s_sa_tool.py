from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import ToolMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Literal
import json
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from langchain.chat_models import init_chat_model
from .k8s_sa_prompts import SERVICE_ACCOUNT_SYSTEM_PROMPT, SERVICE_ACCOUNT_USER_PROMPT

sa_generator_logger = AgentLogger("ServiceAccountGenerator")

class RbacManifest(BaseModel):
    """Individual RBAC manifest"""
    kind: Literal["ServiceAccount", "Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"]
    name: str
    namespace: str | None
    yaml_content: str

class ServiceAccountGenerationOutput(BaseModel):
    """Output schema for ServiceAccount and RBAC generation"""
    
    service_account_yaml: str = Field(
        ...,
        description="ServiceAccount manifest"
    )
    
    role_yaml: str | None = Field(
        None,
        description="Role or ClusterRole manifest"
    )
    
    rolebinding_yaml: str | None = Field(
        None,
        description="RoleBinding or ClusterRoleBinding manifest"
    )
    
    all_manifests: Dict[str, str] = Field(
        ...,
        description="All generated manifests (filename: content)"
    )
    
    manifests: List[RbacManifest] = Field(
        ...,
        description="Structured RBAC manifests"
    )
    
    permissions_summary: str = Field(
        ...,
        description="Human-readable summary of permissions granted"
    )
    
    rules_count: int = Field(
        ...,
        ge=0,
        description="Number of RBAC rules defined"
    )
    
    resources_accessible: List[str] = Field(
        ...,
        description="List of resources accessible"
    )
    
    verbs_allowed: List[str] = Field(
        ...,
        description="List of verbs allowed"
    )
    
    security_score: float = Field(
        ...,
        ge=0,
        le=100,
        description="Security score based on principle of least privilege (0-100)"
    )
    
    security_warnings: List[str] = Field(
        default=[],
        description="Security warnings (overly permissive rules, etc.)"
    )
    
    security_recommendations: List[str] = Field(
        default=[],
        description="Recommendations for tightening permissions"
    )
    
    validation_status: Literal["valid", "warning"]
    validation_messages: List[str] = Field(default=[])
    
    file_names: List[str] = Field(
        ...,
        description="Generated file names"
    )
    
    rbac_scope: str = Field(
        ...,
        description="RBAC scope (namespace/cluster/multi)"
    )
    
    metadata: Dict[str, Any] = Field(default={})
    
    template_variables_used: List[str] = Field(default=[], description="List of template variables used")
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(default=["ServiceAccount"])
    helm_template_functions_used: List[str] = Field(default=[])
    
    @field_validator('file_names')
    @classmethod
    def validate_file_names_not_empty(cls, v):
        """Ensure at least one file generated"""
        if not v or len(v) == 0:
            raise ValueError("At least one manifest file must be generated")
        return v
    
    @field_validator('template_variables_used')
    @classmethod
    def validate_required_variables(cls, v):
        """Ensure critical variables are templated"""
        # ServiceAccount usually requires name to be templated
        return v
    
    class Config:
        extra = "forbid"

@tool
async def generate_service_account_rbac(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate ServiceAccount, Role, and RoleBinding manifests for Kubernetes RBAC.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate ServiceAccount and RBAC manifests")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        k8s_architecture = planner_output.get("kubernetes_architecture", {}) or {}
        
        app_name = parsed_reqs.get("app_name", "myapp")
        namespace = parsed_reqs.get("namespace", "default")
        
        # Extract RBAC configuration from k8s_architecture
        resources = k8s_architecture.get("resources", {}) or {}
        auxiliary_resources = resources.get("auxiliary", []) or []
        
        # Find ServiceAccount configuration
        sa_config = None
        for resource in auxiliary_resources:
            if (resource or {}).get("type", "").lower() == "serviceaccount":
                sa_config = resource
                break
        
        # Default configuration if not found
        if not sa_config:
            sa_config = {
                "name": app_name,
                "rbac_scope": "namespace",
                "permission_profile": "standard"
            }
        
        sa_generator_logger.log_structured(
            level="INFO",
            message="Generating ServiceAccount and RBAC manifests",
            extra={
                "tool_call_id": tool_call_id,
                "app_name": app_name,
                "namespace": namespace
            }
        )
        
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

        formatted_user_query = SERVICE_ACCOUNT_USER_PROMPT.format(
            app_name=app_name,
            namespace=namespace,
            sa_config=escape_json_for_template(json.dumps(sa_config, indent=2)),
            k8s_architecture=escape_json_for_template(json.dumps(k8s_architecture, indent=2)),
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2)
        )
        
        parser = PydanticOutputParser(return_id=True, pydantic_object=ServiceAccountGenerationOutput)
        
        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the ServiceAccountGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        # Remove 'provider' key as it's handled by model_provider or auto-inference
        config_for_init = {k: v for k, v in llm_config.items() if k != 'provider'}
        model = init_chat_model(**config_for_init)
        
        chain = prompt | model | parser
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=SERVICE_ACCOUNT_SYSTEM_PROMPT)]
        })
        
        sa_generator_logger.log_structured(
            level="INFO",
            message="ServiceAccount and RBAC manifests generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        
        response_json = response.model_dump()
        all_manifests = response_json.get("all_manifests", {})
        file_names = response_json.get("file_names", [])
        template_variables_used = response_json.get("template_variables_used", [])
        
        tool_message = ToolMessage(
            content="ServiceAccount and RBAC manifests generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_service_account_rbac" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_service_account_rbac"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_service_account_rbac": {
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
        score = response_json.get("security_score", 0) / 100.0
        generation_metadata["quality_scores"]["generate_service_account_rbac"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "messages": [tool_message],
                "generated_templates": all_manifests,
                "template_variables": template_variables_used,
                # State tracking updates
                "completed_tools": completed_tools,
                "tool_results": tool_results,
                "generation_metadata": generation_metadata,
                "coordinator_state": coordinator_state,
                "next_action": "coordinator"
            },
        )
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
        
        sa_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating ServiceAccount and RBAC manifests: {e}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e
