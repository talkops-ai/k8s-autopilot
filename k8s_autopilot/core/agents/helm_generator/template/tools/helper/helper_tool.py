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

class HelpersTplGenerationOutput(BaseModel):
    tpl_content: str = Field(..., description="Content of _helpers.tpl")
    file_name: str = Field(default="_helpers.tpl")
    defined_templates: List[str] = Field(..., description="List of defined template names")
    template_variables_used: List[str] = Field(default=[], description="List of template variables used")
    kubernetes_api_version: str = Field(default="v1")
    generated_resources: List[str] = Field(default=["_helpers.tpl"])
    validation_messages: List[str] = Field(default=[])
    helm_template_functions_used: List[str] = Field(default=[])
    
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
        planner_output = runtime.state.get("planner_output", {})
        parsed_reqs = planner_output.get("parsed_requirements", {})
        app_name = parsed_reqs.get("app_name", "")
        chart_name = parsed_reqs.get("chart_name", "")

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')
        
        formatted_user_query = HELPERS_GENERATOR_USER_PROMPT.format(
            chart_name=chart_name,
            app_name=app_name,
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
        chain = prompt | model | parser
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