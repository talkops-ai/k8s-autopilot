from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from typing_extensions import Annotated
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Literal
import json
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .service_prompt import (
    SERVICE_GENERATOR_SYSTEM_PROMPT,
    SERVICE_GENERATOR_USER_PROMPT,
)

service_generator_logger = AgentLogger("ServiceGenerator")

class ServiceGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = Field(default="service.yaml")
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    service_endpoints: List[str] = Field(..., description="Expected endpoints")
    metadata: Dict[str, Any] = Field(default={})
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(default=["Service"])
    validation_messages: List[str] = Field(default=[])
    helm_template_functions_used: List[str] = Field(default=[])
    
    @field_validator('template_variables_used')
    def validate_required_variables(cls, v):
        """Ensure critical variables are templated"""
        # Accept either .Values.service.port (singular) or .Values.service.ports (plural)
        # Both are valid patterns depending on whether single or multiple ports are used
        required_vars_options = [
            ['.Values.service.port'],  # Singular port
            ['.Values.service.ports']  # Plural ports (array)
        ]
        
        # Check if at least one of the patterns is present
        has_valid_pattern = any(
            all(var in v for var in option)
            for option in required_vars_options
        )
        
        if not has_valid_pattern:
            raise ValueError(
                f"Missing required template variables. Expected one of: "
                f"{required_vars_options}. Got: {v}"
            )
        return v

    class Config:
        extra = "forbid"

def get_service_details(k8s_architecture: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get service details from k8s architecture.
    
    Args:
        k8s_architecture: The k8s architecture dictionary.
    
    Returns:
        A dictionary containing the service details.
    """
    resources = (k8s_architecture or {}).get("resources", {})
    auxiliary_resources = resources.get("auxiliary", []) or []

    default_response = {
        "service_type": "ClusterIP",
        "port": None,
        "selector": {},
        "configuration_hints": {},
        "why_needed": None,
        "criticality": None,
    }

    for resource in auxiliary_resources:
        resource_type = (resource or {}).get("type", "")
        if resource_type.lower() != "service":
            continue

        config_hints = resource.get("configuration_hints", {}) or {}
        return {
            "service_type": config_hints.get("type", default_response["service_type"]),
            "port": config_hints.get("port"),
            "selector": config_hints.get("selector", {}),
            "configuration_hints": config_hints,
            "why_needed": resource.get("why_needed"),
            "criticality": resource.get("criticality"),
        }

    return default_response

@tool
async def generate_service_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
    ) -> Command:
    """
    Generate a service.yaml file for a Helm chart.
    
    Args:
        runtime: The runtime object for the tool.
        tool_call_id: The ID of the tool call.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate service YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        app_analysis = planner_output.get("application_analysis", {}) or {}
        k8s_architecture = planner_output.get("kubernetes_architecture", {}) or {}

        ## Servuce Level Information
        app_name = parsed_reqs.get("app_name", "myapp")
        service_info = parsed_reqs.get("service", {}) or {}
        service_type = service_info.get("access_type", "ClusterIP")
        ports = service_info.get("port", "")
        app_network = app_analysis.get("network", {}) or {}

        ## Kubernetes Architecture Information
        service_details = get_service_details(k8s_architecture)
        selector_labels = service_details.get("selector", {})
        selector_labels_str = ", ".join([f"{k}:{v}" for k, v in selector_labels.items()])

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

        formatted_user_query = SERVICE_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            service_type=service_type,
            ports=ports,
            selector_labels=selector_labels_str,
            extra_service_details=escape_json_for_template(json.dumps(service_details)),
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2)
        )
        parser = PydanticOutputParser(return_id=True, pydantic_object=ServiceGenerationOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the ServiceGenerationOutput schema.")
        ]).partial(format_instructions=parser.get_format_instructions())

        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        service_generator_logger.log_structured(
            level="INFO",
            message="Generating Service YAML file for Helm chart",
            extra={
                "app_name": app_name,
                "service_type": service_type,
                "ports": ports,
                "selector_labels": selector_labels_str,
                "extra_service_details": service_details,
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
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=SERVICE_GENERATOR_SYSTEM_PROMPT)]
        })
        service_generator_logger.log_structured(
            level="INFO",
            message="Service YAML file generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        service_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        template_variable = response_json.get("template_variables_used", [])
        tool_message = ToolMessage(
            content="Service YAML file generated successfully.",
            tool_call_id=tool_call_id
        )
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_service_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_service_yaml"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_service_yaml": {
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
        generation_metadata["quality_scores"]["generate_service_yaml"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "messages": [tool_message],
                "template_variables": template_variable,
                "generated_templates": {
                    file_name: service_yaml
                },
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
        
        service_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating service YAML: {e}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e