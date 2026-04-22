from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from typing_extensions import Annotated
from pydantic import BaseModel, Field, field_validator
from typing import List, Literal, Optional
from enum import Enum
import json
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerState
REQUIREMENT_PARSER_SYSTEM_PROMPT = """
You are a Kubernetes deployment requirements parser. Extract structured requirements from user inputs into the ParsedRequirements schema.

**INPUTS:**
1. User Requirements: Initial deployment request (framework/language/dependencies)
2. Questions Asked: The deployment questions that were posed to the user
3. Additional Requirements: User's answers to those questions

**EXTRACTION RULES:**

**From User Requirements:**
- Framework (fastapi, express, django, spring-boot, etc.)
- Language (python, nodejs, java, go, etc.)
- App Type (inferred: "fastapi" → language:"python", app_type:"api_service")
- App Name (optional: extract if explicitly mentioned. If missing, infer from container image repository name)
- App Env (optional: extract if mentioned, e.g., "prod", "staging")
- Databases/Services (postgresql, redis, rabbitmq, etc.)

**From Q&A Responses (Map answers to topics regardless of numbering):**

| Topic | Extract To |
|-------|-----------|
| **Image** | `image.full_image`, `image.repository`, `image.tag` |
| **Replicas** | `deployment.min_replicas`, `deployment.max_replicas`, `deployment.high_availability` |
| **Resources** | `resources.cpu_request`, `resources.memory_request` (normalize: "500m", "1Gi") |
| **Access** | `service.access_type` (ingress/loadbalancer/nodeport), `service.port`, `service.target_port` |
| **Ingress** | `service.access_type`="ingress", extract hostname to `service` metadata if possible |
| **Namespace** | `namespace.name`, `namespace.namespace_type` (production/staging/development), `namespace.team` |
| **Config** | `configuration.environment_variables`, `configuration.secrets_mentioned` |
| **Storage** | `deployment` (stateful?), `configuration.config_files` |

**ENVIRONMENT VARIABLE INFERENCE RULES:**
- **Sensitive Vars** (DB_USER, DB_PASSWORD, API_KEY, SECRET):
  - Set `from_secret` = True
  - Set `value` = "app-secrets" (default secret name if not provided)
  - Set `key` = Variable Name (e.g., DB_USER)
- **Non-Sensitive Vars** (DB_HOST, URL, PORT, ENV):
  - Set `from_configmap` = True
  - Set `value` = "app-config" (default configmap name if not provided)
  - Set `key` = Variable Name (e.g., DB_HOST)
- **Literal Values** (if user says "set FOO=bar"):
  - Set `value` = "bar"
  - Set `from_secret` = False, `from_configmap` = False

**FORMAT NORMALIZATION:**
- CPU: "500m", "0.5", "1 core" → normalize to "500m", "1", etc.
- Memory: "1GB", "1Gi", "512MB" → normalize to "1Gi", "512Mi", etc.
- Image: "sandeep2014/aws-orchestrator-agent:latest" → repository:"sandeep2014/aws-orchestrator-agent", tag:"latest"
- Service Access: "Ingress at api.example.com" → access_type:"ingress"

**EXTRACTION PRECEDENCE:**
1. Use Questions context to understand what each answer addresses
2. Additional Requirements override User Requirements when both exist
3. Specificity wins: use most detailed value

**INFERENCE RULES (Use your brain for defaults):**
- **Replicas (if missing):**
  - If env="prod" or "production": Set min_replicas=2, max_replicas=5 (for HA)
  - If env="dev" or "staging": Set min_replicas=1, max_replicas=2
  - If app_type="cronjob" or "daemon": Set min_replicas=1, max_replicas=1
  - Default: min_replicas=1, max_replicas=3

**HANDLING NEGATIVE ASSERTIONS:**
- If user says "no database", "no external services", "no storage", etc.:
  - Set corresponding list/field to empty list or null.
  - Do NOT leave it as "unspecified" if explicitly denied.

**CRITICAL RULES:**
- Extract stated information accurately
- If unspecified, leave as null (EXCEPT for replicas where you should infer defaults based on context)
- Preserve ambiguity in additional_notes
- Handle flexible answer formats (informal language, various numbering)
- Map answer to question using semantic matching if numbering unclear

**OUTPUT:**
Return ParsedRequirements with all fields populated from extraction. Use schema defaults only for fields with default_factory. All optional fields should be null if not mentioned.
"""

REQUIREMENT_PARSER_USER_PROMPT = """
Extract structured Kubernetes/Helm deployment requirements from the following inputs. Use the questions asked as context to accurately parse the user's responses.

**Original User Requirements:**
{user_requirements}

**Questions Asked to User (Context for Parsing):**
{questions_asked}

**User's Responses (Additional Requirements):**
{additional_requirements}

---

Extract all deployment requirements above into ParsedRequirements schema. Return valid JSON.
"""

CLASSIFY_COMPLEXITY_SYSTEM_PROMPT = """
You are an expert in Kubernetes and Helm chart complexity assessment. Your responsibility is to analyze parsed application requirements and determine the complexity classification of the resulting Helm chart deployment.

**Input Sources and Formats:**
- **Parsed Requirements:** Structured details of application requirements provided by the requirements parser.
- **Data Format:** Input may be presented in either JSON or TOON (Token-Oriented Object Notation). Both represent structured data; process them as equivalent and according to their structure.

**Complexity Classification Criteria:**

- **SIMPLE:**
  - Single application component (no external databases or services)
  - Stateless deployment
  - Basic configuration: fixed number of replicas, single region
  - Minimal to no security requirements
  - No special Kubernetes features (e.g., sidecars, init containers)
  - _Example:_ Basic web service deployment with environment variables

- **MEDIUM:**
  - 2–3 components (application plus 1–2 databases/services)
  - Combination of stateless and stateful components
  - Moderate deployment features (autoscaling or multi-region or high availability)
  - Some security requirements (RBAC or network policies)
  - May use sidecars or init containers
  - _Example:_ Microservice with PostgreSQL and Redis cache

- **COMPLEX:**
  - 4 or more components (application plus multiple databases/services)
  - Multiple stateful components
  - Advanced deployment features (autoscaling and multi-region and high availability)
  - Comprehensive security (RBAC, network policies, TLS)
  - Multiple specialized Kubernetes features
  - Complex dependencies and orchestration
  - _Example:_ Distributed system using several databases, message queues, and service mesh

**Component Counting Rules:**
- Main application: counts as 1 component
- Each database: counts as 1 component
- Each external service: counts as 1 component

**Factors Increasing Complexity:**
- High availability requirements
- Multi-region deployments
- Canary deployment strategy
- Network policies
- Service mesh integration
- Use of init containers or sidecars
- Custom RBAC policies
- TLS/mTLS encryption
- StatefulSet requirements

**Human Review Recommendations:**
Trigger a recommendation for human review in these situations:
- The complexity is classified as "complex"
- Security requirements call for production-grade policies
- Deployment requires multi-region or high availability
- Total components exceed 5
- Use of custom or advanced Kubernetes features

**Output Verbosity:**
- Respond in at most 2 short paragraphs explaining the classification decision.
- If listing triggers for human review, use at most 4 concise bullets, 1 line each.
- Prioritize complete, actionable answers within the length limits.

If you adopt a polite or respectful tone, do not increase length to restate politeness.
"""

CLASSIFY_COMPLEXITY_USER_PROMPT = """
Analyze the following parsed requirements and classify the complexity:

**Parsed Requirements:**
{parsed_requirements}

**Context / Notes from Supervisor:**
{notes}

**Your Task:**
1. Count the total number of components (application + databases + external services)
2. Identify special considerations that affect complexity
3. Classify the overall complexity level (simple/medium/complex)
4. Provide clear reasoning for the classification
6. Determine if human review is recommended

Provide your analysis in the structured format.
"""

VALIDATE_REQUIREMENTS_SYSTEM_PROMPT = """
You are a Helm chart requirements validation specialist. Your role is to verify that parsed requirements contain all necessary information for successful Helm chart generation.

**Input Sources and Formats:**
- **Parsed Requirements:** Structured details of application requirements provided by the requirements parser tool (includes user's original input + their responses to clarification questions).
- **Complexity Level:** The complexity level of the application requirements provided by the complexity classification tool.
- **Questions Asked:** The clarification questions that were previously asked to the user via request_human_input tool. This may be empty if no questions were asked yet.
- **Data Format:** Input may be presented in either JSON or TOON (Token-Oriented Object Notation). Both represent structured data; process them as equivalent and according to their structure.

**CRITICAL: Question Avoidance Strategy**

**Before generating any clarification questions:**
1. **Review Questions Asked:** Carefully examine the "Questions Asked" input to understand what information was already requested from the user.
2. **Check Answer Coverage:** Verify if the parsed requirements contain answers to the questions that were already asked. Look for:
   - Answers that directly address the asked questions
   - Information that was extracted from user responses
   - Fields that should have been populated from the Q&A session
3. **Identify Gaps:** Only identify missing information that:
   - Was NOT covered by the questions already asked, OR
   - Was asked but NOT properly answered (ambiguous, incomplete, or missing)
4. **Generate NEW Questions Only:** In `clarifications_needed`, ONLY include questions that:
   - Address information gaps NOT covered by previous questions
   - Are genuinely new and necessary for chart generation
   - Do NOT duplicate or rephrase questions that were already asked

**Example:**
- If "Questions Asked" included: "What is the container image name and tag?"
- And parsed_requirements has `image.repository` and `image.tag` populated → DO NOT ask about image again
- If parsed_requirements is missing `image.repository` → This was asked but not answered, so you may need to ask differently or flag as missing_field

**Critical Fields to Validate:**

**Required Fields (CRITICAL - Must be present):**
- app_type: Must be specified
- image: Must have repository and tag (if container deployment)
- deployment.min_replicas: Must be at least 1

**Defaulting Strategy (ACCEPTABLE GAPS):**
If the following are missing, DO NOT fail validation. Assume defaults will be applied in the next phase:
- Resources (CPU/Memory) -> Default to "500m/512Mi"
- Service Port -> Default to standard ports (80/443/8080) based on app type
- Health Checks -> Default to TCP probe
- Storage -> Default to stateless (no PVC)
- Framework/Language -> Optional if image is provided
- deployment.min_replicas -> Default to 1 if missing
- Namespace -> Default to "default" if missing

**When to Request Clarifications (BLOCKERS ONLY):**
- Missing Container Image (if not provided)
- Missing Exposure Port (if Service is required but port unknown)
- Ambiguous Access Type (e.g., "expose it" without specifying Ingress/LB)

**Completeness Checks:**
- If databases are specified, verify types are valid (postgresql, mysql, mongodb, redis, etc.)
- If external_services are specified, verify they have both name and purpose
- If canary_deployment is true, consider if additional config is needed
- If network_policies is true, verify deployment has appropriate settings
- **Namespace Check**: Verify namespace configuration:
  - Namespace name (e.g., `myapp-prod`, `backend-staging`)
  - Namespace type/environment (production, staging, development)
  - Team ownership (optional but recommended)
- **Traefik Ingress Check**: If access_type is "ingress", check for:
  - Hostname (Host rule)
  - TLS requirements (certResolver or secretName)
  - Middlewares needed (BasicAuth, RateLimit, CORS, StripPrefix?)
  - EntryPoints (web, websecure)

**When to Request Clarifications (Only if NOT Already Asked):**
- Ambiguous version requirements (e.g., "latest") - only if version question wasn't asked
- Unspecified replica counts when autoscaling is implied - only if replicas question wasn't asked
- Missing security context when production deployment is indicated - only if security wasn't covered
- Database credentials management strategy - only if not covered in previous questions
- **Traefik Ingress Details** - If ingress is enabled but missing details:
  - "Do you need any Traefik middlewares like RateLimit, BasicAuth, or CORS?"
  - "Should we use a specific certResolver (e.g., letsencrypt) for TLS?"
  - "Which entryPoints should be used (web, websecure)?"
- **Namespace Details** - If namespace information is missing or incomplete:
  - "Which namespace should this application be deployed to? (e.g., myapp-prod, backend-staging)"
  - "What environment type is this namespace? (production/staging/development)"
  - "Which team owns this namespace? (e.g., backend, platform, devops)"
- Resource limits and requests - only if resource question wasn't asked
- Persistent volume requirements for stateful components - only if storage question wasn't asked

**Validation Rules:**
- **Valid=True**: If critical fields are present, even if optional fields (resources, health checks) are missing.
- **Valid=False**: ONLY if a CRITICAL blocker exists (missing image, impossible config).

**Your Task:**
1. Review "Questions Asked" to understand what was already requested
2. Check if parsed requirements answer those questions adequately
3. Check all required fields are present and valid
4. Verify consistency between related fields
5. Identify missing critical information that was NOT covered by previous questions
6. Generate ONLY NEW, specific, actionable clarification questions (avoid duplicates)
7. List any validation errors found
8. Set valid=true only if requirements are complete and consistent AND all previously asked questions have been answered
"""

VALIDATE_REQUIREMENTS_USER_PROMPT = """
Validate the following parsed requirements for completeness and correctness:

**Parsed Requirements:**
{parsed_requirements}

**Complexity Level:**
{complexity_level}

**Questions Asked by Supervisor:**
{questions_asked}

**Context / Notes from Supervisor:**
{notes}

**Instructions:**
1. **First, review "Questions Asked"** - Understand what information was already requested from the user.

2. **Check if questions were answered** - Verify if the parsed requirements contain adequate answers to the questions that were asked. Look for corresponding fields populated in the parsed requirements.

3. **Perform validation** - A {complexity_level} complexity application should have appropriate detail in its requirements.

4. **Check for:**
   - Required fields presence
   - Field value consistency
   - Missing critical information for this complexity level (that was NOT already asked)
   - Ambiguous or unclear specifications
   - Potential configuration conflicts
   - Information gaps that were NOT covered by previous questions

5. **Generate clarifications** - Only include NEW clarification questions in `clarifications_needed` that:
   - Address gaps NOT covered by "Questions Asked"
   - Are necessary for chart generation
   - Do NOT duplicate questions that were already asked
   - **CRITICAL**: Each question in `clarifications_needed` should be:
     * A complete, standalone question (not a fragment)
     * Clear and specific (e.g., "What is the application name to use for the Helm chart?" instead of "app_name?")
     * Focused on a single topic (one question per list item)
     * Human-readable and easy to understand

**Question Formatting Guidelines:**
- Each question should be a complete sentence ending with a question mark
- Be specific about what information is needed (e.g., "What external port should the Service listen on (service.port)?")
- Group related sub-questions together in a single question when appropriate (e.g., "Which entryPoints should be used (e.g., web, websecure) and are any Traefik middlewares required (BasicAuth, RateLimit, CORS, StripPrefix, etc.)?")
- Avoid overly technical jargon; use clear, user-friendly language

Return your validation results with specific details about any issues found. Remember: avoid asking questions that were already asked unless they were not properly answered."""


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
    min_replicas: Optional[int] = Field(
        default=1,
        ge=1,
        description="Minimum number of pod replicas"
    )
    max_replicas: Optional[int] = Field(
        default=10,
        ge=1,
        description="Maximum number of pod replicas for autoscaling"
    )
    regions: Optional[List[str]] = Field(
        default=["us-east-1"],
        description="Deployment regions",
        examples=[["us-east-1", "eu-west-1"]]
    )
    high_availability: Optional[bool] = Field(
        default=False,
        description="Whether high availability is required"
    )
    canary_deployment: Optional[bool] = Field(
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
    value: Optional[str] = Field(None, description="Variable value OR resource name (if from_secret/configmap)")
    key: Optional[str] = Field(None, description="Key within the secret/configmap")
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


class NamespaceType(str, Enum):
    """Type of namespace environment."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    SANDBOX = "sandbox"
    SHARED = "shared"
    NOT_SPECIFIED = "not_specified"


class NamespaceInfo(BaseModel):
    """Namespace configuration - captured from user requirements."""
    name: Optional[str] = Field(
        None,
        description="Namespace name if specified by user (e.g., 'myapp-prod', 'backend-staging')"
    )
    namespace_type: NamespaceType = Field(
        default=NamespaceType.NOT_SPECIFIED,
        description="Type of namespace environment"
    )
    team: Optional[str] = Field(
        None,
        description="Team or owner of the namespace (e.g., 'backend', 'platform', 'devops')"
    )

def _default_deployment_config() -> DeploymentConfig:
    return DeploymentConfig()

def _default_security_config() -> SecurityConfig:
    return SecurityConfig()

class ParsedRequirements(BaseModel):
    """Structured output from requirements parsing."""
    app_type: str = Field(
        ...,
        description="Type of application being deployed",
        examples=["nodejs_microservice", "python_microservice", "java_microservice", "monolith", "daemon", "cronjob", "web_application", "api_service", "database", "cache", "message_queue"]
    )
    app_name: Optional[str] = Field(
        None,
        description="Name of the application",
        examples=["myapp", "my-app", "my-application"]
    )
    app_env: Optional[str] = Field(
        None,
        description="Environment of the application",
        examples=["dev", "staging", "prod"]
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
        default_factory=_default_deployment_config,
        description="Deployment configuration"
    )
    security: Optional[SecurityConfig] = Field(
        default_factory=_default_security_config,
        description="Security requirements"
    )
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
    namespace: Optional[NamespaceInfo] = Field(
        None,
        description="Namespace configuration if specified by user"
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



class EmptyInputSchema(BaseModel):
    pass

class ParseRequirementsSchema(BaseModel):
    """Schema for parse_requirements tool."""
    additional_requirements: Optional[str] = Field(
        default=None, 
        description="Any additional requirements or feedback provided by the user after a clarification question."
    )
    questions_asked: Optional[str] = Field(
        default=None, 
        description="The questions that were presented to the user for clarification, if any."
    )

@tool(args_schema=ParseRequirementsSchema)
async def parse_requirements(
    additional_requirements: Optional[str],
    questions_asked: Optional[str],
    runtime: ToolRuntime[None, HelmPlannerState],
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
        
        if not questions_asked:
            questions_asked = runtime.state.get('question_asked', '') or ''
            
        if not additional_requirements:
            additional_requirements = runtime.state.get('updated_user_requirements', '') or ''
            
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

        requirement_parser_logger.info("Using LLM configuration for requirement parser", extra={
                "llm_provider": llm_config.get('provider'),
                 "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        ) 

        model = create_model(llm_config)
        higher_model = create_model(higher_llm_config)

        chain = prompt | higher_model | parser

        requirement_parser_logger.debug("Executing LLM chain for requirement parsing", extra={
                "prompt_length": len(formatted_user_query),
                "tool_call_id": tool_call_id
            }
        )
        response = chain.invoke({})
        requirement_parser_logger.info("Requirement parsing completed successfully", extra={
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
        requirement_parser_logger.error(f"Error parsing requirements: {e}", extra={
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



class ClassifyComplexitySchema(BaseModel):
    """Schema for classify_complexity tool."""
    notes: Optional[str] = Field(
        default=None, 
        description="Optional notes or context for complexity classification."
    )

@tool(args_schema=ClassifyComplexitySchema)
async def classify_complexity(
    notes: Optional[str],
    runtime: ToolRuntime[None, HelmPlannerState],
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
        parsed_requirements_encoded = json.dumps(parsed_requirements_json)
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = CLASSIFY_COMPLEXITY_USER_PROMPT.format(
            parsed_requirements=escape_json_for_template(parsed_requirements_encoded),
            notes=notes or "No additional notes provided."
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
        requirement_parser_logger.info("Using LLM configuration for complexity classification", extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = create_model(llm_config)
        chain = prompt | model | parser
        requirement_parser_logger.debug("Executing LLM chain for complexity classification", extra={
                "prompt_length": len(formatted_user_query),
                "tool_call_id": tool_call_id
            }
        )
        response = chain.invoke({})
        requirement_parser_logger.info("Complexity classification completed successfully", extra={
                "complexity_level": response.complexity_level,
                "components_count": response.components_count,
                "requires_human_review": response.requires_human_review,
                "tool_call_id": tool_call_id
            })
        
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
        requirement_parser_logger.error(f"Error classifying complexity: {e}", extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to classify complexity: {e}. Please re-run the classify_complexity tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})
    

class ValidateRequirementsSchema(BaseModel):
    """Schema for validate_requirements tool."""
    notes: Optional[str] = Field(
        default=None, 
        description="Optional notes prioritizing specific validation checks."
    )

@tool(args_schema=ValidateRequirementsSchema)
async def validate_requirements(
    notes: Optional[str],
    runtime: ToolRuntime[None, HelmPlannerState],
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
        parsed_requirements_encoded = json.dumps(parsed_requirements_json)
        questions_asked = runtime.state.get('question_asked', '') or ''

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = VALIDATE_REQUIREMENTS_USER_PROMPT.format(
            parsed_requirements=escape_json_for_template(parsed_requirements_encoded),
            complexity_level=complexity_level,
            questions_asked=escape_json_for_template(questions_asked),
            notes=notes or "No additional notes provided."
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
        higher_llm_config = config.get_llm_higher_config()
        requirement_parser_logger.info("Using LLM configuration for requirements validation", extra={
                "llm_provider": llm_config.get('provider'),
                "llm_model": llm_config.get('model'),
                "llm_temperature": llm_config.get('temperature'),
                "llm_max_tokens": llm_config.get('max_tokens')
            }
        )
        model = create_model(llm_config)
        higher_model = create_model(higher_llm_config)
        chain = prompt | higher_model | parser
        requirement_parser_logger.debug("Executing LLM chain for requirements validation", extra={
                "prompt_length": len(formatted_user_query),
                "tool_call_id": tool_call_id
            }
        )
        response = chain.invoke({})
        requirement_parser_logger.info("Requirements validation completed successfully", extra={
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
        requirement_parser_logger.error(f"Error validating requirements: {e}", extra={
                "error": str(e),
                "tool_call_id": tool_call_id
            }
        )
        failed_tool_message = ToolMessage(
            content=f"Failed to validate requirements: {e}. Please re-run the validate_requirements tool.",
            tool_call_id=tool_call_id
        )
        return Command(update={"messages": [failed_tool_message]})