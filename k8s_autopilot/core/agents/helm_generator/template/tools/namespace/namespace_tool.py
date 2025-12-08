from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from pydantic import BaseModel, Field
from typing import Dict, List, Any, Literal
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

class PriorityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================
# OUTPUT SCHEMA
# ============================================================

class NamespaceGenerationOutput(BaseModel):
    """Output schema for Namespace YAML generation"""
    
    yaml_content: str = Field(..., description="Complete Namespace YAML")
    file_name: str = Field(default="namespace.yaml")
    template_variables_used: List[str] = Field(default=[])
    helm_template_functions_used: List[str] = Field(default=[])
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = Field(default=[])
    metadata: Dict[str, Any] = Field(default={})
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(default=["Namespace"])
    namespace_name: str = Field(..., description="The namespace name")

    class Config:
        extra = "forbid"


# ============================================================
# TOOL DEFINITION
# ============================================================

@tool
async def generate_namespace_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate Kubernetes Namespace YAML for a Helm chart.
    
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
        
        # Extract inputs
        app_name = parsed_reqs.get("app_name", "myapp")
        
        # core is now a list - iterate to find Namespace resource
        core_resources_list = k8s_arch.get("resources", {}).get("core", [])
        
        # Find Namespace resource from core list
        namespace_resource = None
        for resource in core_resources_list:
            if resource.get("type") == "Namespace":
                namespace_resource = resource
                break
        
        # Extract namespace configuration
        if namespace_resource:
            key_config = namespace_resource.get("key_configuration_parameters", {})
            namespace_name = key_config.get("name", app_name)
            labels = key_config.get("labels", {})
            team = labels.get("team") or "devops"
            env_from_labels = labels.get("environment")
        else:
            # Fallback if no Namespace resource found
            namespace_name = parsed_reqs.get("namespace", {}).get("name", app_name)
            team = parsed_reqs.get("namespace", {}).get("team", "devops")
            env_from_labels = parsed_reqs.get("namespace", {}).get("namespace_type")
        
        # Determine environment type
        env_type = NamespaceType.PRODUCTION
        if env_from_labels:
            if env_from_labels in ["development", "dev"]:
                env_type = NamespaceType.DEVELOPMENT
            elif env_from_labels in ["staging", "stage"]:
                env_type = NamespaceType.STAGING
            elif env_from_labels in ["production", "prod"]:
                env_type = NamespaceType.PRODUCTION
        elif "dev" in namespace_name.lower():
            env_type = NamespaceType.DEVELOPMENT
        elif "staging" in namespace_name.lower():
            env_type = NamespaceType.STAGING
        
        namespace_generator_tool_logger.log_structured(
            level="INFO",
            message="Generating Namespace YAML",
            extra={
                "tool_call_id": tool_call_id,
                "namespace": namespace_name,
                "type": env_type.value,
                "team": team,
                "found_namespace_resource": namespace_resource is not None
            }
        )
        
        # Extract helper templates from previous tool execution
        tool_results_state = runtime.state.get("tool_results", {})
        helper_output = tool_results_state.get("generate_helpers_tpl", {}).get("output", {})
        template_categories = helper_output.get("template_categories", {})
        
        naming_templates = template_categories.get("naming", [])
        label_templates = template_categories.get("labels", [])
        annotation_templates = template_categories.get("annotations", [])
        
        # Format user query
        formatted_user_query = NAMESPACE_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            namespace_name=namespace_name,
            namespace_type=env_type.value,
            priority_level=PriorityLevel.MEDIUM.value,
            team=team,
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2)
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
