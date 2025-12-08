from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional, Literal
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .ingress_prompts import (
    INGRESS_GENERATOR_SYSTEM_PROMPT,
    INGRESS_GENERATOR_USER_PROMPT,
)

ingress_generator_logger = AgentLogger("IngressGenerator")
class IngressGenerationOutput(BaseModel):
    """Output schema for Ingress YAML generation"""
    
    yaml_content: str = Field(..., description="Complete Ingress YAML with Helm templating")
    file_name: str = Field(default="ingress.yaml", description="Name of the file to be created")
    template_variables_used: List[str] = Field(..., description="List of template variables used")
    helm_template_functions_used: List[str] = Field(default=[], description="List of helm template functions used")
    validation_status: Literal["valid", "warning", "error"] = Field(..., description="Validation status")
    validation_messages: List[str] = Field(default=[], description="Validation warnings or errors")
    metadata: Dict[str, Any] = Field(default={}, description="Metadata about the Ingress")
    
    # Ingress-specific metadata
    dns_requirements: List[str] = Field(
        ..., 
        description="DNS records that need to be created (host -> LoadBalancer IP)"
    )
    certificate_requirements: List[str] = Field(
        default=[],
        description="TLS certificates needed"
    )
    exposed_paths: List[str] = Field(
        default=[],
        description="All exposed URL paths"
    )
    
    @field_validator('yaml_content')
    def validate_yaml_syntax(cls, v):
        """Validate YAML syntax"""
        import yaml
        try:
            yaml.safe_load(v)
        except Exception as e:
            raise ValueError(f"Invalid YAML syntax: {str(e)}")
        return v
    
    class Config:
        extra = "forbid"

@tool
def generate_ingress_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generates Ingress YAML for the given application configuration.
    Process:
    1. Validate input schema
    2. Construct LLM prompt with ingress configuration
    3. Generate YAML with proper Helm templating
    4. Extract DNS and certificate requirements
    5. Return structured output
    Args:
        runtime: The runtime context
        tool_call_id: The tool call ID
    Returns:
        A Command object containing the generated Ingress YAML
    """
    pass