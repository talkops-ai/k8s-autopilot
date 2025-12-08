from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any
import json
from toon import encode
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .values_prompts import (
    VALUES_YAML_GENERATOR_SYSTEM_PROMPT,
    VALUES_YAML_GENERATOR_USER_PROMPT,
)

values_generator_logger = AgentLogger("ValuesGenerator")

class ValuesSection(BaseModel):
    """Represents a section in values.yaml"""
    section_name: str
    values: Dict[str, Any]
    comments: List[str] = Field(default=[])

class ValuesYamlGenerationOutput(BaseModel):
    yaml_content: str = Field(..., description="Complete values.yaml with inline comments")
    file_name: str = Field(default="values.yaml")
    sections: List[ValuesSection] = Field(..., description="Structured sections")
    schema_definition: Dict[str, Any] = Field(..., description="JSON Schema for validation")
    coverage_percentage: float = Field(..., description="% of template vars covered")
    metadata: Dict[str, Any] = Field(default={})
    
    @field_validator('coverage_percentage')
    @classmethod
    def validate_coverage(cls, v):
        if v < 95.0:
            # Warning: Coverage below 95%
            pass
        return v
    
@tool
async def generate_values_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
    ) -> Command:
    """
    Generates values.yaml by analyzing all generated templates.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate values.yaml")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        app_analysis = planner_output.get("application_analysis", {}) or {}
        app_name = parsed_reqs.get("app_name", "myapp")
        all_template_variables = runtime.state.get("template_variables", []) or []
        generated_templates = runtime.state.get("generated_templates", {}) or {}
        app_analysis_encode = encode(app_analysis) if app_analysis else ""

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')
        
        formatted_user_query = VALUES_YAML_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            all_template_variables=escape_json_for_template(json.dumps(all_template_variables)),
            total_variables=len(all_template_variables),
            app_analysis=escape_json_for_template(app_analysis_encode),
            generated_templates=escape_json_for_template(json.dumps(generated_templates)),
        )
        values_generator_logger.log_structured(
            level="INFO",
            message="Generating Values YAML file for Helm chart",
            extra={
                "app_name": app_name,
                "total_variables": len(all_template_variables),
                "tool_call_id": tool_call_id
            }
        )
        parser = PydanticOutputParser(return_id=True, pydantic_object=ValuesYamlGenerationOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the ValuesYamlGenerationOutput schema.")
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
            "system_message": [SystemMessage(content=VALUES_YAML_GENERATOR_SYSTEM_PROMPT)]
        })
        values_generator_logger.log_structured(
            level="INFO",
            message="Values YAML file generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        values_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        tool_message = ToolMessage(
            content="Values YAML file generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_values_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_values_yaml"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_values_yaml": {
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
        score = response_json.get("coverage_percentage", 0) / 100.0
        generation_metadata["quality_scores"]["generate_values_yaml"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "messages": [tool_message],
                "generated_templates": {
                    file_name: values_yaml
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
        
        values_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating Values YAML file: {e}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e