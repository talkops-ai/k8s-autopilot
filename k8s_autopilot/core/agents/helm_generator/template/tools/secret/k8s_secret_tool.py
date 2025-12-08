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
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .k8s_secrets_prompts import SECRET_SYSTEM_PROMPT, SECRET_USER_PROMPT

secret_generator_logger = AgentLogger("SecretGenerator")

class SecretManifest(BaseModel):
    """Secret manifest"""
    name: str
    namespace: str
    secret_type: str
    yaml_content: str

class SecretGenerationOutput(BaseModel):
    """Output schema for Secret generation"""
    
    secret_yaml: str = Field(
        ...,
        description="Secret manifest YAML"
    )
    
    external_secret_yaml: str | None = Field(
        None,
        description="ExternalSecret manifest (if ESO enabled)"
    )
    
    all_manifests: Dict[str, str] = Field(
        ...,
        description="All generated manifests"
    )
    
    manifests: List[SecretManifest] = Field(
        ...,
        description="Structured manifests"
    )
    
    secret_keys: List[str] = Field(
        ...,
        description="List of secret keys"
    )
    
    secret_size_bytes: int = Field(
        ...,
        ge=0,
        description="Approximate size of secret data"
    )
    
    security_score: float = Field(
        ...,
        ge=0,
        le=100,
        description="Security score (0-100)"
    )
    
    security_warnings: List[str] = Field(
        default=[],
        description="Security warnings"
    )
    
    security_recommendations: List[str] = Field(
        default=[],
        description="Security recommendations"
    )
    
    validation_status: Literal["valid", "warning"]
    validation_messages: List[str] = Field(default=[])
    
    usage_examples: Dict[str, str] = Field(
        default={},
        description="Examples of how to reference this secret"
    )
    
    env_var_example: str | None = Field(
        None,
        description="Example environment variable reference"
    )
    
    volume_mount_example: str | None = Field(
        None,
        description="Example volume mount reference"
    )
    
    file_names: List[str] = Field(
        ...,
        description="Generated file names"
    )
    
    metadata: Dict[str, Any] = Field(
        default={},
        description="Additional metadata"
    )
    
    template_variables_used: List[str] = Field(default=[], description="List of template variables used")
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(default=["Secret"])
    helm_template_functions_used: List[str] = Field(default=[])
    
    @field_validator('secret_keys')
    @classmethod
    def validate_secret_keys_not_empty(cls, v):
        """Ensure at least one secret key"""
        if not v or len(v) == 0:
            raise ValueError("At least one secret key required")
        return v
    
    @field_validator('template_variables_used')
    @classmethod
    def validate_required_variables(cls, v):
        """Ensure critical variables are templated"""
        # Secrets usually require name to be templated
        return v
    
    class Config:
        extra = "forbid"

@tool
async def generate_secret(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate Kubernetes Secret manifests for sensitive data management.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate Secret manifests")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        k8s_architecture = planner_output.get("kubernetes_architecture", {}) or {}
        
        app_name = parsed_reqs.get("app_name", "myapp")
        namespace = parsed_reqs.get("namespace", "default")
        
        # Extract Secret configuration from k8s_architecture
        resources = k8s_architecture.get("resources", {}) or {}
        auxiliary_resources = resources.get("auxiliary", []) or []
        
        # Find Secret configuration
        secret_config = None
        for resource in auxiliary_resources:
            if (resource or {}).get("type", "").lower() == "secret":
                secret_config = resource
                break
        
        # Default configuration if not found
        if not secret_config:
            secret_config = {
                "name": f"{app_name}-secret",
                "secret_type": "Opaque",
                "keys": ["password", "api-key"]
            }
        
        secret_generator_logger.log_structured(
            level="INFO",
            message="Generating Secret manifests",
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

        formatted_user_query = SECRET_USER_PROMPT.format(
            app_name=app_name,
            namespace=namespace,
            secret_config=escape_json_for_template(json.dumps(secret_config, indent=2)),
            k8s_architecture=escape_json_for_template(json.dumps(k8s_architecture, indent=2)),
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2)
        )
        
        parser = PydanticOutputParser(return_id=True, pydantic_object=SecretGenerationOutput)
        
        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the SecretGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        
        
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
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
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=SECRET_SYSTEM_PROMPT)]
        })
        
        secret_generator_logger.log_structured(
            level="INFO",
            message="Secret manifests generated successfully",
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
            content="Secret manifests generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_secret" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_secret"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_secret": {
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
        generation_metadata["quality_scores"]["generate_secret"] = score

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
        
        secret_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating Secret manifests: {e}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e
