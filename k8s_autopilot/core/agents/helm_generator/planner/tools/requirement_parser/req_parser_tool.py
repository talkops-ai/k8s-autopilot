from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from typing_extensions import Annotated
from pydantic import BaseModel, Field, field_validator
from typing import List, Literal, Optional
from enum import Enum
from toon import encode
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from k8s_autopilot.core.state.base import PlanningSwarmState
from .req_parser_prompts import (
    REQUIREMENT_PARSER_SYSTEM_PROMPT, 
    REQUIREMENT_PARSER_USER_PROMPT,
    CLASSIFY_COMPLEXITY_SYSTEM_PROMPT,
    CLASSIFY_COMPLEXITY_USER_PROMPT,
    VALIDATE_REQUIREMENTS_USER_PROMPT,
    VALIDATE_REQUIREMENTS_SYSTEM_PROMPT
)
# Create agent logger for requirement parser
requirement_parser_logger = AgentLogger("k8sAutopilotRequirementParser")

class DatabaseRequirement(BaseModel):
    """Database configuration requirements."""
    type: str = Field(
        ...,
        description="Database type (e.g., postgresql, mysql, mongodb, redis)",
        examples=["postgresql", "mysql", "mongodb"]
    )
    version: str = Field(
        ...,
        description="Database version requirement",
        examples=["13.x", "8.0", "5.0"]
    )
    purpose: Literal["primary", "replica", "cache"] = Field(
        ...,
        description="Role of this database in the architecture"
    )

class ExternalService(BaseModel):
    """External service dependency."""
    name: str = Field(
        ...,
        description="Name of the external service",
        examples=["redis", "rabbitmq", "elasticsearch"]
    )
    purpose: str = Field(
        ...,
        description="Purpose of this service in the application",
        examples=["caching", "message queue", "search"]
    )

class DeploymentConfig(BaseModel):
    """Deployment configuration requirements."""
    min_replicas: int = Field(
        default=1,
        ge=1,
        description="Minimum number of pod replicas"
    )
    max_replicas: int = Field(
        default=10,
        ge=1,
        description="Maximum number of pod replicas for autoscaling"
    )
    regions: List[str] = Field(
        default=["us-east-1"],
        description="Deployment regions",
        examples=[["us-east-1", "eu-west-1"]]
    )
    high_availability: bool = Field(
        default=False,
        description="Whether high availability is required"
    )
    canary_deployment: bool = Field(
        default=False,
        description="Whether canary deployment strategy should be used"
    )

class SecurityConfig(BaseModel):
    """Security requirements configuration."""
    network_policies: bool = Field(
        default=False,
        description="Whether Kubernetes network policies should be enforced"
    )
    pod_security_policy: Literal["privileged", "baseline", "restricted"] = Field(
        default="baseline",
        description="Pod security policy standard to apply"
    )
    rbac_required: bool = Field(
        default=False,
        description="Whether RBAC (Role-Based Access Control) is required"
    )
    tls_encryption: bool = Field(
        default=False,
        description="Whether TLS encryption should be enabled"
    )

class ComplexityClassification(BaseModel):
    """Output schema for complexity classification."""
    complexity_level: Literal["simple", "medium", "complex"] = Field(
        ...,
        description="Overall complexity classification of the Helm chart requirements"
    )
    reasoning: str = Field(
        ...,
        description="Detailed explanation of why this complexity level was assigned",
        min_length=20
    )
    components_count: int = Field(
        ...,
        ge=0,
        description="Total number of distinct components (app, databases, services)"
    )
    special_considerations: List[str] = Field(
        default=[],
        description="List of special features or challenges that affect complexity",
        examples=[["High availability", "Network policies", "Multi-region deployment"]]
    )

    requires_human_review: bool = Field(
        ...,
        description="Whether human review is recommended before deployment"
    )

class ContainerImageInfo(BaseModel):
    """Container image information - only if provided by user."""
    full_image: Optional[str] = Field(
        None,
        description="Full image string as provided by user (e.g., 'sandeep2014/aws-orchestrator-agent:latest')"
    )
    repository: Optional[str] = Field(
        None,
        description="Parsed repository name"
    )
    tag: Optional[str] = Field(
        None,
        description="Parsed tag"
    )


class ServiceAccessType(str, Enum):
    """How the application should be accessed."""
    INGRESS = "ingress"
    LOAD_BALANCER = "loadbalancer"
    CLUSTER_IP = "clusterip"
    NODE_PORT = "nodeport"
    NOT_SPECIFIED = "not_specified"

class ServiceInfo(BaseModel):
    """Service access configuration - captured from Q5."""
    access_type: ServiceAccessType = Field(
        default=ServiceAccessType.NOT_SPECIFIED,
        description="How the app should be accessed"
    )
    port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="Service port if specified"
    )
    target_port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="Container port if specified"
    )

class EnvironmentVariableInfo(BaseModel):
    """Single environment variable."""
    name: str = Field(..., description="Variable name")
    value: Optional[str] = Field(None, description="Variable value if provided")
    from_secret: bool = Field(
        default=False,
        description="Whether this should come from a secret"
    )
    from_configmap: bool = Field(
        default=False,
        description="Whether this should come from a configmap"
    )

class ConfigurationInfo(BaseModel):
    """Environment variables and configuration - from Q6."""
    environment_variables: List[EnvironmentVariableInfo] = Field(
        default=[],
        description="List of environment variables mentioned"
    )
    secrets_mentioned: List[str] = Field(
        default=[],
        description="Names of secrets mentioned"
    )
    configmaps_mentioned: List[str] = Field(
        default=[],
        description="Names of configmaps mentioned"
    )
    config_files: List[str] = Field(
        default=[],
        description="Configuration files mentioned"
    )

class ResourceInfo(BaseModel):
    """CPU and Memory requirements - only if user provides them."""
    cpu_request: Optional[str] = Field(
        None,
        description="CPU request as mentioned by user (e.g., '500m', '1', '2 cores')"
    )
    memory_request: Optional[str] = Field(
        None,
        description="Memory request as mentioned by user (e.g., '512Mi', '1Gi', '1GB')"
    )
    cpu_limit: Optional[str] = Field(
        None,
        description="CPU limit if specified"
    )
    memory_limit: Optional[str] = Field(
        None,
        description="Memory limit if specified"
    )

class ParsedRequirements(BaseModel):
    """Structured output from requirements parsing."""
    app_type: str = Field(
        ...,
        description="Type of application being deployed",
        examples=["nodejs_microservice", "python_microservice", "java_microservice", "monolith", "daemon", "cronjob", "web_application", "api_service", "database", "cache", "message_queue"]
    )
    framework: str = Field(
        ...,
        description="Application framework",
        examples=["express", "fastapi", "spring-boot", "django"]
    )
    language: str = Field(
        ...,
        description="Programming language",
        examples=["nodejs", "python", "java", "go", "ruby", "php", "scala", "kotlin", "rust", "elixir", "erlang"]
    )
    databases: List[DatabaseRequirement] = Field(
        default=[],
        description="List of database requirements"
    )
    external_services: List[ExternalService] = Field(
        default=[],
        description="List of external service dependencies"
    )
    deployment: DeploymentConfig = Field(
        default_factory=DeploymentConfig,
        description="Deployment configuration"
    )
    security: SecurityConfig = Field(
        default_factory=SecurityConfig,
        description="Security requirements"
    ),
    image: Optional[ContainerImageInfo] = Field(
        None,
        description="Container image details from Q2"
    )
    service: Optional[ServiceInfo] = Field(
        None,
        description="Service access configuration from Q5"
    )
    configuration: Optional[ConfigurationInfo] = Field(
        None,
        description="Environment variables and config from Q6"
    )

    resources: Optional[ResourceInfo] = Field(
        None,
        description="CPU and memory requirements from Q4"
    )

class ValidationResult(BaseModel):
    """Output schema for requirements validation."""
    valid: bool = Field(
        ...,
        description="Whether the requirements are complete and valid for Helm chart generation"
    )
    missing_fields: List[str] = Field(
        default=[],
        description="List of critical fields that are missing or incomplete",
        examples=[["framework", "language", "deployment.regions"]]
    )
    clarifications_needed: List[str] = Field(
        default=[],
        description="List of questions or clarifications needed from the user",
        examples=[["What is the expected traffic volume?", "Which database version is required?"]]
    )
    validation_errors: List[str] = Field(
        default=[],
        description="List of validation errors or inconsistencies found",
        examples=[["max_replicas must be greater than min_replicas", "Invalid database type specified"]]
    )
    
    @field_validator('valid')
    @classmethod
    def check_validity(cls, v: bool, info) -> bool:
        """Ensure valid is False if there are any errors or missing fields."""
        data = info.data
        if v and (data.get('missing_fields') or data.get('validation_errors')):
            return False
        return v



@tool
async def parse_requirements(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],

    ) -> Command:
    """
    Parse natural language into structured format.
    
    Extract application type, framework, language, databases, external services,
    deployment configuration, and security requirements from user input.
    
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update the state with the parsed requirements
    """
    try:
        user_requirements = runtime.state.get('user_query', '')
        additional_requirements = runtime.state.get('updated_user_requirements', '') or ''
        questions_asked = runtime.state.get('question_asked', '') or ''
        # additional_requirements_encoded = encode(additional_requirements)
        # Format user prompt with actual data, escaping curly braces in JSON
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            if not json_str:
                return ''  # Return empty string if input is None or empty
            return json_str.replace('{', '{{').replace('}', '}}')
        
        formatted_user_query = REQUIREMENT_PARSER_USER_PROMPT.format(
            user_requirements=escape_json_for_template(user_requirements),
            additional_requirements=escape_json_for_template(additional_requirements),
            questions_asked=escape_json_for_template(questions_asked)
        )
        parser = PydanticOutputParser(pydantic_object=ParsedRequirements)
        escaped_system_prompt = REQUIREMENT_PARSER_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the ParsedRequirements schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())

        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()

        requirement_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for requirement parser",
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

        higher_model = LLMProvider.create_llm(
            provider=higher_llm_config['provider'],
            model=higher_llm_config['model'],
            temperature=higher_llm_config['temperature'],
            max_tokens=higher_llm_config['max_tokens']
        )

        chain = prompt | model | parser

        requirement_parser_logger.log_structured(            
            level="DEBUG",
            message="Executing LLM chain for requirement parsing",
            extra={
                "prompt_length": len(formatted_user_query),
                "tool_call_id": tool_call_id
            }
        )
        response = chain.invoke({})
        requirement_parser_logger.log_structured(
            level="INFO",
            message="Requirement parsing completed successfully",
            extra={
                "app_type": response.app_type,
                "framework": response.framework,
                "language": response.language,
                "databases_count": len(response.databases),
                "external_services_count": len(response.external_services),
                "tool_call_id": tool_call_id
            }
        )
        
        # Store parsed requirements in handoff_data as both JSON string and dict for flexibility
        handoff_data = {  # JSON string for serialization
            "parsed_requirements": response.model_dump(),  # Dict for easy access
        }
        
        # Tool message: Inform LLM that tool completed successfully and to proceed with next tool
        tool_message_content = "Requirements parsed successfully. Proceed with classify_complexity tool."
        tool_message = ToolMessage(content=tool_message_content, tool_call_id=tool_call_id)
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )

    except Exception as e:
        requirement_parser_logger.log_structured(
            level="ERROR",
            message=f"Error parsing requirements: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(content=f"Failed to parse requirements: {e}, so please re ran the parse requirement tools once again.", tool_call_id=tool_call_id)
        return Command(
            update={
                "messages": [failed_tool_message],
            },
        )



@tool
async def classify_complexity(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Classify the complexity of the Helm chart requirements.
    
    Assess if deployment is simple/medium/complex based on component count,
    features, security needs, and special Kubernetes requirements.
    
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID
    Returns:
        Command: Command to update state with complexity classification
    """
    try:
        # Retrieve parsed requirements from handoff_data
        handoff_data = runtime.state.get('handoff_data', {})
        parsed_requirements_json = handoff_data.get('parsed_requirements')
        parsed_requirements_encoded = encode(parsed_requirements_json)
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = CLASSIFY_COMPLEXITY_USER_PROMPT.format(
            parsed_requirements=escape_json_for_template(parsed_requirements_encoded)
        )
        parser = PydanticOutputParser(pydantic_object=ComplexityClassification)
        escaped_system_prompt = CLASSIFY_COMPLEXITY_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the ComplexityClassification schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())

        config = Config()
        llm_config = config.get_llm_config()
        requirement_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for complexity classification",
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
        requirement_parser_logger.log_structured(
            level="DEBUG",
            message="Executing LLM chain for complexity classification",
            extra={
                "prompt_length": len(formatted_user_query),
                "tool_call_id": tool_call_id
            }
        )
        response = chain.invoke({})
        requirement_parser_logger.log_structured(
            level="INFO",
            message="Complexity classification completed successfully",
            extra={
                "complexity_level": response.complexity_level,
                "components_count": response.components_count,
                "requires_human_review": response.requires_human_review,
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data with complexity classification (merge with existing)
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,  # Preserve existing data
            "complexity_classification": response.model_dump(),
        }
        
        # Tool message: Inform LLM that tool completed successfully and to proceed with next tool
        tool_message_content = "Complexity classification completed successfully. Proceed with validate_requirements tool."
        tool_message = ToolMessage(content=tool_message_content, tool_call_id=tool_call_id)
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )
    except Exception as e:
        requirement_parser_logger.log_structured(
            level="ERROR",
            message=f"Error classifying complexity: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to classify complexity: {e}. Please re-run the classify_complexity tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})
    

@tool
async def validate_requirements(
    runtime: ToolRuntime[None, PlanningSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
    """
    Validate the requirements for Helm chart generation.
    
    Check completeness, flag missing fields, identify conflicts, 
    and verify all necessary information is present for chart generation.
    
    Args:
        runtime: Tool runtime from the planning swarm
        tool_call_id: Injected tool call ID 
    Returns:
        Command: Command to update state with validation result
    """
    try:
        # Retrieve parsed requirements and complexity from handoff_data
        handoff_data = runtime.state.get('handoff_data', {})
        parsed_requirements_json = handoff_data.get('parsed_requirements')
        complexity_json = handoff_data.get('complexity_classification')
        complexity_level = complexity_json.get('complexity_level') if complexity_json else "unknown"
        parsed_requirements_encoded = encode(parsed_requirements_json)
        questions_asked = runtime.state.get('question_asked', '') or ''

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = VALIDATE_REQUIREMENTS_USER_PROMPT.format(
            parsed_requirements=escape_json_for_template(parsed_requirements_encoded),
            complexity_level=complexity_level,
            questions_asked=escape_json_for_template(questions_asked)
        )
        parser = PydanticOutputParser(pydantic_object=ValidationResult)
        escaped_system_prompt = VALIDATE_REQUIREMENTS_SYSTEM_PROMPT.replace('{', '{{').replace('}', '}}')
        prompt = ChatPromptTemplate.from_messages([
            ("system", escaped_system_prompt),
            ("user", formatted_user_query),
            ("user", "Please respond with valid JSON matching the ValidationResult schema:\n{format_instructions}")
        ]).partial(format_instructions=parser.get_format_instructions())

        config = Config()
        llm_config = config.get_llm_config()
        requirement_parser_logger.log_structured(
            level="INFO",
            message="Using LLM configuration for requirements validation",
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
        requirement_parser_logger.log_structured(
            level="DEBUG",
            message="Executing LLM chain for requirements validation",
            extra={
                "prompt_length": len(formatted_user_query),
                "tool_call_id": tool_call_id
            }
        )
        response = chain.invoke({})
        requirement_parser_logger.log_structured(
            level="INFO",
            message="Requirements validation completed successfully",
            extra={
                "valid": response.valid,
                "missing_fields_count": len(response.missing_fields),
                "validation_errors_count": len(response.validation_errors),
                "clarifications_needed_count": len(response.clarifications_needed),
                "tool_call_id": tool_call_id
            }
        )
        
        # Update handoff_data with validation result (merge with existing)
        existing_handoff_data = runtime.state.get('handoff_data', {})
        handoff_data = {
            **existing_handoff_data,  # Preserve existing data
            "validation_result": response.model_dump(),
        }
        
        # Tool message: Inform LLM that tool completed successfully
        tool_message_content = "Requirements validation completed successfully. All requirements are valid. Proceed to architecture planning."
        tool_message = ToolMessage(content=tool_message_content, tool_call_id=tool_call_id)
        
        return Command(
            update={
                "handoff_data": handoff_data,
                "messages": [tool_message],
            },
        )
    except Exception as e:
        requirement_parser_logger.log_structured(
            level="ERROR",
            message=f"Error validating requirements: {e}",
            extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to validate requirements: {e}. Please re-run the validate_requirements tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})