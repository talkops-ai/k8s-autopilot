"""
NetworkPolicy YAML generation tool. Must be implemented in the future version. This is a placeholder for the future version.

This tool is not yet implemented in the current version. It is a placeholder for the future version.

"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Literal
import json
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .nw_policy_prompts import NETWORKPOLICY_GENERATOR_SYSTEM_PROMPT, NETWORK_POLICY_GENERATOR_USER_PROMPT

nw_policy_generator_tool_logger = AgentLogger("NetworkPolicyGeneratorTool")

class NetworkPolicyGenerationToolOutput(BaseModel):
    """Output schema for NetworkPolicy YAML generation"""
    
    yaml_content: str = Field(..., description="Complete NetworkPolicy YAML")
    file_name: str = Field(default="network-policy.yaml")
    template_variables_used: List[str]
    helm_template_functions_used: List[str] = Field(default=[])
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = Field(default=[])
    metadata: Dict[str, Any] = Field(default={})
    
    # NetworkPolicy-specific
    policy_summary: str = Field(
        ...,
        description="Human-readable summary of policy (e.g., 'Allows ingress on port 8080 from same namespace')"
    )
    affected_pods: str = Field(
        ...,
        description="Description of which pods are affected by this policy"
    )
    
    @field_validator('yaml_content')
    @classmethod
    def validate_yaml_syntax(cls, v):
        from yaml import safe_load
        try:
            parsed = safe_load(v)
            if parsed.get('kind') != 'NetworkPolicy':
                raise ValueError("Generated YAML is not a NetworkPolicy resource")
        except Exception as e:
            raise ValueError(f"Invalid YAML: {str(e)}")
        return v
    
    class Config:
        extra = "forbid"

@tool
async def generate_network_policy_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate a NetworkPolicy YAML file for a Helm chart.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate NetworkPolicy YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        security_policy = planner_output.get("security_policy", {}) or {}
        app_name = parsed_reqs.get("app_name", "myapp")
        network_policy = security_policy.get("network_policies", {}) or {}
        network_policy_labels = network_policy.get("labels", {}) or {}
        network_policy_ingress_rules = network_policy.get("ingress_rules", []) or []
        network_policy_egress_rules = network_policy.get("egress_rules", []) or []
        network_policy_preset_policy = network_policy.get("preset_policy", None)

        nw_policy_generator_tool_logger.log_structured(
            level="INFO",
            message="Generating NetworkPolicy YAML",
            extra={
                "tool_call_id": tool_call_id,
                "app_name": app_name,
                "network_policy_labels": network_policy_labels,
                "network_policy_ingress_rules": network_policy_ingress_rules,
                "network_policy_egress_rules": network_policy_egress_rules,
                "network_policy_preset_policy": network_policy_preset_policy,
            }
        )
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')
        network_policy_labels_str = escape_json_for_template(json.dumps(network_policy_labels))
        network_policy_policy_types_str = escape_json_for_template(json.dumps(network_policy))
        network_policy_ingress_rules_str = escape_json_for_template(json.dumps(network_policy_ingress_rules))
        network_policy_egress_rules_str = escape_json_for_template(json.dumps(network_policy_egress_rules))
        formatted_user_query = NETWORK_POLICY_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            policy_name=network_policy,
            pod_selector=network_policy_labels_str,
            policy_types=network_policy_policy_types_str,
            ingress_rules=network_policy_ingress_rules_str,
            egress_rules=network_policy_egress_rules_str,
            preset_policy=network_policy_preset_policy,
        )
        parser = PydanticOutputParser(pydantic_object=NetworkPolicyGenerationToolOutput)
        escaped_system_prompt = NETWORKPOLICY_GENERATOR_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the NetworkPolicyGenerationToolOutput schema.")
        ]).partial(format_instructions=parser.get_format_instructions())

        config = Config()
        llm_config = config.get_llm_config()
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )
        chain = prompt | model | parser
        response = await chain.ainvoke({})
        nw_policy_generator_tool_logger.log_structured(
            level="INFO",
            message="NetworkPolicy YAML generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        network_policy_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        template_variable = response_json.get("template_variables_used", [])
        tool_message = ToolMessage(
            content="NetworkPolicy YAML file generated successfully.",
            tool_call_id=tool_call_id
        )
        return Command(
            update={
                "messages": [tool_message],
                "generated_templates": {
                    file_name: network_policy_yaml
                },
                "template_variables": template_variable
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
        
        nw_policy_generator_tool_logger.log_structured(
            level="ERROR",
            message=f"Error generating NetworkPolicy YAML: {str(e)}",
            extra=error_context
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to generate NetworkPolicy YAML: {e}. Please re-run the tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})  
