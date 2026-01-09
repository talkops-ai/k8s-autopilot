from pydantic import BaseModel, Field, field_validator
from typing import List, Any
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from langchain.chat_models import init_chat_model
from .helper_prompts import (
    HELPERS_GENERATOR_SYSTEM_PROMPT,
    HELPERS_GENERATOR_USER_PROMPT,
)

# Create agent logger for helper tool
helper_generator_logger = AgentLogger("HelperGenerator")


class HelpersTplGenerationOutput(BaseModel):
    """Output schema for _helpers.tpl generation."""
    tpl_content: str = Field(..., description="Content of _helpers.tpl")
    file_name: str = Field(default="_helpers.tpl")
    defined_templates: List[str] = Field(..., description="List of defined template names")
    validation_messages: List[str] = Field(default=[])
    
    @field_validator('defined_templates')
    @classmethod
    def validate_defined_templates(cls, v):
        """Ensure essential templates are defined."""
        required_suffixes = ['name', 'fullname', 'chart', 'labels', 'selectorLabels', 'serviceAccountName']
        found_suffixes = [t.split('.')[-1] for t in v if '.' in t]
        
        missing = [s for s in required_suffixes if s not in found_suffixes]
        if missing:
            # Log warning but don't fail - LLM might use slightly different naming
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
    Generate a minimal _helpers.tpl file for a Helm chart.
    
    This tool generates ONLY the essential helper templates that are actually
    used by other Helm templates:
    - CHARTNAME.name
    - CHARTNAME.fullname
    - CHARTNAME.chart
    - CHARTNAME.labels
    - CHARTNAME.selectorLabels
    - CHARTNAME.serviceAccountName
    
    Args:
        runtime: The runtime object for the tool.
        tool_call_id: The ID of the tool call.
    
    Returns:
        Command: Updates state with generated _helpers.tpl content.
    """
    try:
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        
        # Extract app_name with fallback logic
        # Handle case where app_name might be None (not just missing)
        app_name_raw = parsed_reqs.get("app_name")
        
        # If app_name is None or empty, try to derive from other sources
        if not app_name_raw:
            # Try namespace name as fallback
            namespace_info = parsed_reqs.get("namespace", {})
            if namespace_info and namespace_info.get("name"):
                app_name = namespace_info.get("name")
            else:
                # Try to extract from image repository name
                image_info = parsed_reqs.get("image", {})
                if image_info:
                    repository = image_info.get("repository", "")
                    if repository:
                        # Extract app name from repository (e.g., "talkops/eagle" -> "eagle")
                        app_name = repository.split("/")[-1] if "/" in repository else repository
                    else:
                        app_name = "my-app"
                else:
                    app_name = "my-app"
        else:
            app_name = app_name_raw
        
        # Extract chart_name with fallback to app_name
        chart_name = parsed_reqs.get("chart_name") or app_name
        
        # Ensure we have a valid string before sanitizing
        if not chart_name or not isinstance(chart_name, str):
            chart_name = app_name if app_name and isinstance(app_name, str) else "my-app"
        
        # Ensure app_name is also a valid string
        if not app_name or not isinstance(app_name, str):
            app_name = chart_name if chart_name and isinstance(chart_name, str) else "my-app"
        
        # Sanitize chart name for Helm template naming
        # Replace spaces and special chars with hyphens
        chart_name = chart_name.lower().replace(" ", "-").replace("_", "-")
        
        helper_generator_logger.log_structured(
            level="INFO",
            message="Generating minimal _helpers.tpl file",
            extra={
                "app_name": app_name,
                "chart_name": chart_name,
                "tool_call_id": tool_call_id
            }
        )
        
        # Format user prompt with minimal info needed
        formatted_user_query = HELPERS_GENERATOR_USER_PROMPT.format(
            chart_name=chart_name,
            app_name=app_name
        )
        
        parser = PydanticOutputParser(pydantic_object=HelpersTplGenerationOutput)
        
        # Escape curly braces for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Build prompt template
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the HelpersTplGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        
        # Get LLM configuration
        config = Config()
        higher_llm_config = config.get_llm_higher_config()
        
        # Remove 'provider' key as it's handled by model_provider or auto-inference
        higher_config_for_init = {k: v for k, v in higher_llm_config.items() if k != 'provider'}
        higher_model = init_chat_model(**higher_config_for_init)
        
        # Create and execute the chain
        chain = prompt | higher_model | parser
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=HELPERS_GENERATOR_SYSTEM_PROMPT)]
        })
        
        helper_generator_logger.log_structured(
            level="INFO",
            message="_helpers.tpl file generated successfully",
            extra={
                "defined_templates": response.defined_templates,
                "tool_call_id": tool_call_id
            }
        )
        
        response_json = response.model_dump()
        tpl_content = response_json.get("tpl_content", "")
        file_name = response_json.get("file_name", "_helpers.tpl")

        tool_message = ToolMessage(
            content=f"_helpers.tpl file generated with {len(response.defined_templates)} templates.",
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
        generation_metadata["quality_scores"]["generate_helpers_tpl"] = 1.0

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
        raise e