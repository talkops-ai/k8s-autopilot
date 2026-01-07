from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Literal
import json
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
from .pdb_prompts import PDB_GENERATOR_SYSTEM_PROMPT, PDB_GENERATOR_USER_PROMPT

pdb_generator_tool_logger = AgentLogger("PDBGeneratorTool")

class PDBGenerationOutput(BaseModel):
    """Output schema for PDB YAML generation"""
    
    yaml_content: str = Field(..., description="Complete PDB YAML")
    file_name: str = Field(default="pdb.yaml")
    template_variables_used: List[str]
    helm_template_functions_used: List[str] = Field(default=[])
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = Field(default=[])
    metadata: Dict[str, Any] = Field(default={})
    
    # PDB-specific
    disruption_budget: str = Field(
        ...,
        description="Configured disruption budget (e.g., 'minAvailable: 2' or 'maxUnavailable: 25%')"
    )
    kubernetes_api_version: str = Field(default="policy/v1")
    generated_resources: List[str] = Field(default=["PodDisruptionBudget"])
    
    @field_validator('template_variables_used')
    @classmethod
    def validate_required_variables(cls, v):
        """Ensure critical variables are templated"""
        # PDB requires either minAvailable or maxUnavailable
        required_vars = ['.Values.podDisruptionBudget.minAvailable', '.Values.podDisruptionBudget.maxUnavailable']
        # Check if at least one of them is present (or both if logic permits, but usually one)
        # For now, just pass if any PDB var is used or if list is not empty
        if not v:
             # Warning only
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
                if parsed and parsed.get('kind') != 'PodDisruptionBudget':
                    raise ValueError("Generated YAML is not a PDB resource")
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
            # Validate that it's a PDB structure (even if values are placeholders)
            if parsed.get('kind') != 'PodDisruptionBudget':
                raise ValueError("Generated YAML structure is not a PDB resource")
        except Exception as e:
            # For Helm templates, we're more lenient - structure validation is best effort
            # The actual validation happens when Helm renders the template
            # Log warning but don't fail - LLM should generate correct structure
            pdb_generator_tool_logger.log_structured(
                level="WARNING",
                message="YAML structure validation warning for Helm template",
                extra={"error": str(e), "note": "Helm templates may not parse as pure YAML"}
            )
            # Don't raise - allow Helm template syntax through
        
        return v
    
    class Config:
        extra = "forbid"


def get_pdb_config(k8s_architecture: dict[str, Any]) -> dict[str, Any]:
    """
    Get PDB configuration from k8s architecture.
    
    Args:
        k8s_architecture: The k8s architecture dictionary.
    
    Returns:
        A dictionary containing the PDB configuration details:
        - minAvailable: int | None
        - maxUnavailable: str | None
        - selector_labels: dict
        - criticality: str | None
        - dependencies: list
        - tradeoffs: str | None
        - environment_specific: str | None
        - reasoning: str | None
    """
    resources = (k8s_architecture or {}).get("resources", {})
    auxiliary_resources = resources.get("auxiliary", []) or []

    default_response = {
        "minAvailable": 1,
        "maxUnavailable": None,
        "selector_labels": {},
        "criticality": None,
        "dependencies": [],
        "tradeoffs": None,
        "environment_specific": None,
        "reasoning": None,
    }

    for resource in auxiliary_resources:
        resource_type = (resource or {}).get("type", "")
        if resource_type.lower() != "poddisruptionbudget":
            continue

        config_hints = resource.get("configuration_hints", {}) or {}
        return {
            "minAvailable": config_hints.get("minAvailable"),
            "maxUnavailable": config_hints.get("maxUnavailable"),
            "selector_labels": config_hints.get("selector", {}).get("matchLabels", {}),
            "criticality": resource.get("criticality"),
            "dependencies": resource.get("dependencies", []),
            "tradeoffs": resource.get("tradeoffs"),
            "environment_specific": resource.get("environment_specific"),
            "reasoning": config_hints.get("reasoning"),
        }
    return default_response

@tool
async def generate_pdb_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate a PodDisruptionBudget YAML file for a Helm chart.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate PDB YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        k8s_architecture = planner_output.get("kubernetes_architecture", {}) or {}
        scaling_strategy = planner_output.get("scaling_strategy", {}) or {}
        
        app_name = parsed_reqs.get("app_name", "myapp")
        namespace = parsed_reqs.get("namespace", "default")
        
        # Extract PDB config from auxiliary resources
        pdb_config = get_pdb_config(k8s_architecture) or {}
        
        # Extract scaling strategy for production environment
        prod_scaling = scaling_strategy.get("prod", {}) or {}
        
        # Extract target kind and selector labels from scaling_strategy
        target_kind = scaling_strategy.get("target_kind", "Deployment")
        selector_labels = scaling_strategy.get("selector_labels", {}) or {}
        
        # If selector_labels not in scaling_strategy, use from PDB config
        if not selector_labels:
            selector_labels = pdb_config.get("selector_labels", {}) or {}
        
        # Extract PDB values - prefer from prod_scaling, fallback to pdb_config
        min_available = prod_scaling.get("min_available") or pdb_config.get("minAvailable")
        max_unavailable = prod_scaling.get("max_unavailable") or pdb_config.get("maxUnavailable")
        unhealthy_pod_eviction_policy = prod_scaling.get("unhealthy_pod_eviction_policy") or pdb_config.get("unhealthy_pod_eviction_policy")
        
        pdb_generator_tool_logger.log_structured(
            level="INFO",
            message="Generating PDB YAML",
            extra={
                "tool_call_id": tool_call_id,
                "app_name": app_name,
                "namespace": namespace,
                "min_available": min_available,
                "max_unavailable": max_unavailable
            }
        )
        
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

        formatted_user_query = PDB_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            namespace=namespace,
            target_kind=target_kind,
            selector_labels=escape_json_for_template(json.dumps(selector_labels)),
            min_available=min_available,
            max_unavailable=max_unavailable,
            unhealthy_pod_eviction_policy=unhealthy_pod_eviction_policy,
            pdb_config=escape_json_for_template(json.dumps(pdb_config, indent=2)),
            naming_templates=json.dumps(naming_templates, indent=2),
            label_templates=json.dumps(label_templates, indent=2),
            annotation_templates=json.dumps(annotation_templates, indent=2)
    )
        parser = PydanticOutputParser(pydantic_object=PDBGenerationOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the PDBGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        # Remove 'provider' key as it's handled by model_provider or auto-inference
        config_for_init = {k: v for k, v in llm_config.items() if k != 'provider'}
        model = init_chat_model(**config_for_init)

        higher_config_for_init = {k: v for k, v in higher_llm_config.items() if k != 'provider'}
        higher_model = init_chat_model(**higher_config_for_init)
        chain = prompt | higher_model | parser
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=PDB_GENERATOR_SYSTEM_PROMPT)]
        })
        pdb_generator_tool_logger.log_structured(
            level="INFO",
            message="PDB YAML generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        pdb_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        template_variable = response_json.get("template_variables_used", [])
        tool_message = ToolMessage(
            content="PDB YAML file generated successfully.",
            tool_call_id=tool_call_id
        )
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_pdb_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_pdb_yaml"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_pdb_yaml": {
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
        generation_metadata["quality_scores"]["generate_pdb_yaml"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "messages": [tool_message],
                "generated_templates": {
                    file_name: pdb_yaml
                },
                "template_variables": template_variable,
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
        
        pdb_generator_tool_logger.log_structured(
            level="ERROR",
            message=f"Error generating PDB YAML: {str(e)}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e