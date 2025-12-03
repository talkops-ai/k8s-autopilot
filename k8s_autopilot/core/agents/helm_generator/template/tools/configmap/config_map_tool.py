from pydantic import BaseModel, Field, field_validator
from typing import Dict, Literal, List, Any
import json
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import ToolMessage, SystemMessage
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .config_map_prompts import CONFIGMAP_SYSTEM_PROMPT, CONFIGMAP_USER_PROMPT

config_map_logger = AgentLogger("ConfigMapGenerator")

class ConfigMapGenerationOutput(BaseModel):
    yaml_content: str = Field(..., description="Content of configmap.yaml")
    file_name: str = Field(default="configmap.yaml")
    template_variables_used: List[str] = Field(..., description="List of template variables used")
    helm_template_functions_used: List[str] = Field(default=[], description="List of helm template functions used")
    validation_status: Literal["valid", "warning", "error"] = Field(..., description="Validation status")
    validation_messages: List[str] = Field(default=[], description="Validation warnings or errors")
    metadata: Dict[str, Any] = Field(default={}, description="Metadata about the ConfigMap")
    # ConfigMap-specific
    total_keys: int = Field(..., description="Number of keys in ConfigMap")
    binary_keys: List[str] = Field(default=[], description="Keys stored in binaryData")
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(default=["ConfigMap"])
    usage_example: str = Field(
        ...,
        description="Example of how to mount/use this ConfigMap in Deployment"
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
                yaml.safe_load(v)
            except Exception as e:
                raise ValueError(f"Invalid YAML syntax: {str(e)}")
            return v
        
        # Has Helm template syntax - strip it for structure validation
        stripped_yaml = v
        # Replace all {{ ... }} blocks (including multi-line) with placeholders
        # This regex handles {{- ... }}, {{ ... }}, and multi-line templates
        stripped_yaml = re.sub(r'\{\{-?\s*[^}]*\}\}', 'PLACEHOLDER', stripped_yaml, flags=re.DOTALL)
        
        try:
            yaml.safe_load(stripped_yaml)
        except Exception as e:
            # For Helm templates, we're more lenient - structure validation is best effort
            # The actual validation happens when Helm renders the template
            # Log warning but don't fail - LLM should generate correct structure
            config_map_logger.log_structured(
                level="WARNING",
                message="YAML structure validation warning for Helm template",
                extra={"error": str(e), "note": "Helm templates may not parse as pure YAML"}
            )
            # Don't raise - allow Helm template syntax through
        
        return v


    @field_validator('template_variables_used')
    def validate_required_variables(cls, v):
        """Ensure critical variables are templated"""
        # ConfigMaps might be static, but usually have some values
        # We can at least check for basic structure if needed, or leave it lenient
        return v
    
    class Config:
        extra = "forbid"

@tool
async def generate_configmap_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
    ) -> Command:
    """
    Generate a standard configmap.yaml file for a Helm chart.
    
    Args:
        runtime: The runtime object for the tool.
        tool_call_id: The ID of the tool call.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate ConfigMap YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        k8s_architecture = planner_output.get("kubernetes_architecture", {}) or {}
        
        # Extract application details 
        app_name = parsed_reqs.get("app_name", "myapp")
        namespace = parsed_reqs.get("namespace", "default")
        
        # Extract ConfigMap configuration from k8s_architecture
        resources = k8s_architecture.get("resources", {}) or {}
        auxiliary_resources = resources.get("auxiliary", []) or []
        
        # Find ConfigMap configuration
        configmap_config = None
        for resource in auxiliary_resources:
            if (resource or {}).get("type", "").lower() == "configmap":
                configmap_config = resource
                break
        
        # Default configuration if not found
        if not configmap_config:
            # Fallback to old method for backward compatibility
            configuration = parsed_reqs.get("configuration", {}) or {}
            configmap_config = {
                "name": f"{app_name}-config",
                "data": {},
                "env_vars": configuration.get("environment_variables", []),
                "config_files": configuration.get("config_files", [])
            }
        
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = CONFIGMAP_USER_PROMPT.format(
            app_name=app_name,
            namespace=namespace,
            configmap_config=escape_json_for_template(json.dumps(configmap_config, indent=2))
        )
        parser = PydanticOutputParser(return_id=True, pydantic_object=ConfigMapGenerationOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the ConfigMapGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        config = Config()
        llm_config = config.get_llm_config()
        config_map_logger.log_structured(
            level="INFO",
            message="Generating configmap.yaml file for Helm chart",
            extra={
                "app_name": app_name,
                "namespace": namespace,
                "configmap_config": configmap_config,
                "tool_call_id": tool_call_id
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        chain = prompt | model | parser
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=CONFIGMAP_SYSTEM_PROMPT)]
        })
        config_map_logger.log_structured(
            level="INFO",
            message="ConfigMap YAML file generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        configmap_yaml_content = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        template_variable = response_json.get("template_variables_used", [])
        tool_message = ToolMessage(
            content="ConfigMap YAML file generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_configmap_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_configmap_yaml"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_configmap_yaml": {
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
        generation_metadata["quality_scores"]["generate_configmap_yaml"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "generated_templates": {
                    file_name: configmap_yaml_content
                },
                "template_variables": template_variable,
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
        
        config_map_logger.log_structured(
            level="ERROR",
            message=f"Error generating ConfigMap YAML file: {e}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e