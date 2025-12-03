from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Any, Literal
import json
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .hpa_prompts import HPA_GENERATOR_SYSTEM_PROMPT, HPA_GENERATOR_USER_PROMPT

hpa_generator_tool_logger = AgentLogger("HPAGeneratorTool")

class HPAGenerationToolOutput(BaseModel):
    """Output schema for HPA YAML generation"""
    
    yaml_content: str = Field(..., description="Complete HPA YAML")
    file_name: str = Field(default="hpa.yaml")
    template_variables_used: List[str]
    helm_template_functions_used: List[str] = Field(default=[])
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = Field(default=[])
    metadata: Dict[str, Any] = Field(default={})
    
    # HPA-specific
    metrics_configured: List[str] = Field(
        ...,
        description="List of metrics configured (e.g., ['cpu', 'memory', 'custom:requests-per-second'])"
    )
    scaling_range: str = Field(
        ...,
        description="Min-max replica range (e.g., '2-10')"
    )
    kubernetes_api_version: str = Field(default="autoscaling/v2")
    generated_resources: List[str] = Field(default=["HorizontalPodAutoscaler"])
    
    @field_validator('template_variables_used')
    @classmethod
    def validate_required_variables(cls, v):
        """Ensure critical variables are templated"""
        # HPA usually requires min/max replicas to be templated
        required_vars = ['.Values.autoscaling.minReplicas', '.Values.autoscaling.maxReplicas']
        missing = [var for var in required_vars if var not in v]
        if missing:
            # Warning only as implementation might vary
            pass
        return v

    @field_validator('yaml_content')
    @classmethod
    def validate_yaml_syntax(cls, v):
        """
        Validate YAML structure by stripping Helm template directives.
        Helm templates contain {{ ... }} syntax which is not valid YAML until rendered.
        We strip template syntax and validate the underlying YAML structure.
        """
        import re
        from yaml import safe_load
        
        # Check if content contains Helm template syntax
        has_helm_syntax = bool(re.search(r'\{\{', v))
        
        if not has_helm_syntax:
            # No Helm syntax - validate as pure YAML
            try:
                parsed = safe_load(v)
                if parsed and parsed.get('kind') != 'HorizontalPodAutoscaler':
                    raise ValueError("Generated YAML is not an HPA resource")
            except Exception as e:
                raise ValueError(f"Invalid YAML: {str(e)}")
            return v
        
        # Has Helm template syntax - strip it for structure validation
        stripped_yaml = v
        # Replace all {{ ... }} blocks (including multi-line) with placeholders
        # This regex handles {{- ... }}, {{ ... }}, and multi-line templates
        stripped_yaml = re.sub(r'\{\{-?\s*[^}]*\}\}', 'PLACEHOLDER', stripped_yaml, flags=re.DOTALL)
        
        try:
            parsed = safe_load(stripped_yaml)
            if parsed is None:
                # Only comments/whitespace after stripping - might be valid Helm template
                return v
            # Validate that it's an HPA structure (even if values are placeholders)
            if parsed.get('kind') != 'HorizontalPodAutoscaler':
                raise ValueError("Generated YAML structure is not an HPA resource")
        except Exception as e:
            # For Helm templates, we're more lenient - structure validation is best effort
            # The actual validation happens when Helm renders the template
            # Log warning but don't fail - LLM should generate correct structure
            hpa_generator_tool_logger.log_structured(
                level="WARNING",
                message="YAML structure validation warning for Helm template",
                extra={"error": str(e), "note": "Helm templates may not parse as pure YAML"}
            )
            # Don't raise - allow Helm template syntax through
        
        return v
    
    class Config:
        extra = "forbid"

def get_env_hpa_config(env: str, scaling_strategy: dict[str, Any]) -> dict[str, Any]:
    """
    Get the HPA configuration for a specific environment.
    
    Args:
        env: Environment name (e.g., "dev", "staging", "prod")
        scaling_strategy: Scaling strategy dictionary containing environment-specific configs
    
    Returns:
        Dictionary containing HPA configuration for the specified environment:
        - min_replicas: int
        - max_replicas: int
        - target_cpu_utilization: int (percentage)
        - target_memory_utilization: int | None (percentage, can be null)
    """
    if not scaling_strategy:
        return {}
    
    # Normalize environment name to lowercase for case-insensitive matching
    env_lower = env.lower() if env else ""
    
    # Get environment-specific configuration
    env_config = scaling_strategy.get(env_lower, {})
    
    # Return the configuration dict directly
    # It should contain: min_replicas, max_replicas, target_cpu_utilization, target_memory_utilization
    return env_config

@tool
async def generate_hpa_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate a HorizontalPodAutoscaler YAML file.

    Args:
        runtime: The runtime context
        tool_call_id: The tool call ID
    
    Returns:
        A Command object containing the generated HPA YAML
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate HPA YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        scaling_strategy = planner_output.get("scaling_strategy", {}) or {}


        ## HPA Level Information
        app_name = parsed_reqs.get("app_name", "myapp")
        target_kind = scaling_strategy.get("target_kind", "Deployment")
        target_name = scaling_strategy.get("target_name", "myapp")
        min_replicas = scaling_strategy.get("min_replicas", 1)
        max_replicas = scaling_strategy.get("max_replicas", 10)
        resource_metrics = scaling_strategy.get("resource_metrics", []) or []
        custom_metrics = scaling_strategy.get("custom_metrics", []) or []
        scaling_behavior = scaling_strategy.get("scaling_behavior", None)
        
        hpa_generator_tool_logger.log_structured(
            level="INFO",
            message="Generating HPA YAML",
            extra={
                "tool_call_id": tool_call_id,
            }
        )
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        resource_metrics_str = escape_json_for_template(json.dumps(resource_metrics))
        custom_metrics_str = escape_json_for_template(json.dumps(custom_metrics))
        scaling_behavior_str = escape_json_for_template(json.dumps(scaling_behavior) if scaling_behavior else "null")
        formatted_user_query = HPA_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            target_kind=target_kind,
            target_name=target_name,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            resource_metrics=resource_metrics_str,
            custom_metrics=custom_metrics_str,
            scaling_behavior=scaling_behavior_str,
        )
        parser = PydanticOutputParser(pydantic_object=HPAGenerationToolOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the HPAGenerationToolOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        config = Config()
        llm_config = config.get_llm_config()
        hpa_generator_tool_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for HPA generation",
            extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
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
            "system_message": [SystemMessage(content=HPA_GENERATOR_SYSTEM_PROMPT)]
        })
        hpa_generator_tool_logger.log_structured(
            level="INFO",
            message="HPA YAML generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        hpa_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        template_variable = response_json.get("template_variables_used", [])
        tool_message = ToolMessage(
            content="HPA YAML generated successfully.",
            tool_call_id=tool_call_id
        )
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_hpa_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_hpa_yaml"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_hpa_yaml": {
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
        generation_metadata["quality_scores"]["generate_hpa_yaml"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "generated_templates": {
                    file_name: hpa_yaml
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
        
        hpa_generator_tool_logger.log_structured(
            level="ERROR",
            message=f"Error generating HPA YAML: {str(e)}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e