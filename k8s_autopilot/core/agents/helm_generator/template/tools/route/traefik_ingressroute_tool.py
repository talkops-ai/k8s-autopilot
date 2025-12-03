from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional, Literal
import json
from .traefik_ingressroute_prompts import TRAEFIK_INGRESSROUTE_SYSTEM_PROMPT, TRAEFIK_INGRESSROUTE_USER_PROMPT

traefik_ingressroute_tool_logger = AgentLogger("TraefikIngressRouteTool")

class TraefikIngressRouteToolOutput(BaseModel):
    """Output schema for Traefik IngressRoute generation"""
    yaml_content: str = Field(
        ...,
        description="Complete IngressRoute YAML with Helm templating",
        min_length=100
    )

    file_name: str = Field(
        ...,
        pattern=r'^(ingressroute|ingressroutetcp|ingressrouteudp)\.yaml$',
        description="Generated file name based on route type"
    )

    middleware_yaml: Optional[str] = Field(
        None,
        description="Generated Middleware/MiddlewareTCP resources (if any)"
    )
    
    traefik_service_yaml: Optional[str] = Field(
        None,
        description="Generated TraefikService resource (if advanced LB used)"
    )
    
    template_variables_used: List[str] = Field(
        ...,
        description="All {{ .Values.* }} references"
    )
    
    helm_template_functions_used: List[str] = Field(default=[])
    
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = Field(default=[], description="Validation messages")
    
    metadata: Dict[str, Any] = Field(default={}, description="Metadata")
    
    # Traefik-specific metadata
    route_type: str = Field(..., description="HTTP/TCP/UDP")
    matcher_rules: List[str] = Field(..., description="Traefik matcher rules")
    entry_points: List[str] = Field(..., description="Traefik entry points")
    middlewares_referenced: List[str] = Field(default=[], description="Traefik middlewares referenced")
    services_referenced: List[str] = Field(default=[], description="Traefik services referenced")
    tls_enabled: bool = Field(..., description="TLS enabled")
    cert_resolver_used: Optional[str] = Field(None, description="Cert resolver used")
    
    @field_validator('yaml_content')
    @classmethod
    def validate_yaml_syntax(cls, v):
        """
        Validate YAML structure by stripping Helm template directives.
        Helm templates contain {{ ... }} syntax which is not valid YAML until rendered.
        We strip template syntax and validate the underlying YAML structure.
        """
        import re
        import yaml
        
        # Check if content contains Helm template syntax
        has_helm_syntax = bool(re.search(r'\{\{', v))
        
        if not has_helm_syntax:
            # No Helm syntax - validate as pure YAML
            try:
                parsed = yaml.safe_load(v)
                if parsed and parsed.get('kind') not in ['IngressRoute', 'IngressRouteTCP', 'IngressRouteUDP']:
                    raise ValueError("Invalid Traefik resource kind")
            except Exception as e:
                raise ValueError(f"Invalid YAML syntax: {str(e)}")
            return v
        
        # Has Helm template syntax - strip it for structure validation
        stripped_yaml = v
        # Replace all {{ ... }} blocks (including multi-line) with placeholders
        # This regex handles {{- ... }}, {{ ... }}, and multi-line templates
        stripped_yaml = re.sub(r'\{\{-?\s*[^}]*\}\}', 'PLACEHOLDER', stripped_yaml, flags=re.DOTALL)
        
        try:
            parsed = yaml.safe_load(stripped_yaml)
            if parsed is None:
                # Only comments/whitespace after stripping - might be valid Helm template
                return v
            # Validate that it's a Traefik resource structure (even if values are placeholders)
            if parsed.get('kind') not in ['IngressRoute', 'IngressRouteTCP', 'IngressRouteUDP']:
                raise ValueError("Generated YAML structure is not a valid Traefik resource kind")
        except Exception as e:
            # For Helm templates, we're more lenient - structure validation is best effort
            # The actual validation happens when Helm renders the template
            # Log warning but don't fail - LLM should generate correct structure
            traefik_ingressroute_tool_logger.log_structured(
                level="WARNING",
                message="YAML structure validation warning for Helm template",
                extra={"error": str(e), "note": "Helm templates may not parse as pure YAML"}
            )
            # Don't raise - allow Helm template syntax through
        
        return v
    
    @field_validator('template_variables_used')
    @classmethod
    def validate_minimum_templating(cls, v):
        """
        Ensure proper Helm templating.
        For IngressRoute, we allow fewer variables since helper functions (include) 
        are also valid templating and tracked separately in helm_template_functions_used.
        """
        if not v or len(v) < 1:
            # Require at least 1 template variable (e.g., .Values.service.port)
            # Helper functions like {{ include }} are tracked separately
            raise ValueError("Insufficient Helm templating detected - at least one template variable required")
        return v

    class Config:
        extra = "forbid"

@tool
async def generate_traefik_ingressroute_yaml(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """
    Generate a Traefik IngressRoute YAML file for a Helm chart.
    """
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate Traefik IngressRoute YAML")
        
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        k8s_arch = planner_output.get("kubernetes_architecture", {}) or {}
        
        # Extract app name from parsed requirements
        ingress_traefik_app_name = parsed_reqs.get("app_name", "myapp")
        
        # Find Ingress resource in kubernetes_architecture.resources.auxiliary
        ingress_resource = None
        resources = k8s_arch.get("resources", {}) or {}
        auxiliary_resources = resources.get("auxiliary", []) or []
        for resource in auxiliary_resources:
            if (resource or {}).get("type") == "Ingress":
                ingress_resource = resource
                break
        
        if not ingress_resource:
            raise ValueError("No Ingress resource found in kubernetes_architecture")
        
        config_hints = (ingress_resource or {}).get("configuration_hints", {}) or {}
        
        # Extract basic configuration
        ingress_traefik_namespace = config_hints.get("namespace", "default")
        hosts = config_hints.get("hosts", []) or []
        rules = config_hints.get("rules", []) or []
        tls_config = config_hints.get("tls", {}) or {}
        traefik_config = config_hints.get("traefik", {}) or {}
        
        # Determine route type (HTTP/TCP/UDP) - default to HTTP for standard Ingress
        ingress_traefik_route_type = "HTTP"
        
        # Build Traefik matcher rules from hosts and paths
        ingress_traefik_routes = []
        ingress_traefik_services = []
        
        for rule in rules:
            rule_dict = rule or {}
            host = rule_dict.get("host", "")
            http_config = rule_dict.get("http", {}) or {}
            paths = http_config.get("paths", []) or []
            
            for path_config in paths:
                path_dict = path_config or {}
                path = path_dict.get("path", "/")
                path_type = path_dict.get("pathType", "Prefix")
                service_config = path_dict.get("service", {}) or {}
                
                # Build Traefik matcher rule
                matcher_parts = []
                if host:
                    matcher_parts.append(f"Host(`{host}`)")
                
                if path and path != "/":
                    if path_type == "Prefix":
                        matcher_parts.append(f"PathPrefix(`{path}`)")
                    else:
                        matcher_parts.append(f"Path(`{path}`)")
                
                matcher = " && ".join(matcher_parts) if matcher_parts else f"Host(`{host}`)"
                ingress_traefik_routes.append(matcher)
                
                # Extract service information
                service_name = service_config.get("name", "")
                service_port_dict = service_config.get("port", {}) or {}
                service_port = service_port_dict.get("number", 80)
                
                if service_name:
                    ingress_traefik_services.append({
                        "name": service_name,
                        "port": service_port,
                        "weight": 1
                    })
        
        # Extract TLS configuration
        ingress_traefik_tls = {
            "enabled": tls_config.get("enabled", False),
            "secret_name": tls_config.get("secretName", ""),
            "cert_resolver": tls_config.get("certResolver", ""),
            "passthrough": False
        }
        
        # Extract Traefik-specific configuration from annotations
        annotations = traefik_config.get("annotations", {}) or {}
        ingress_traefik_entry_points = []
        
        # Parse entry points from annotations
        entrypoints_annotation = annotations.get("traefik.ingress.kubernetes.io/router.entrypoints", "")
        if entrypoints_annotation:
            ingress_traefik_entry_points = [entrypoints_annotation]
        else:
            # Default based on TLS
            ingress_traefik_entry_points = ["websecure"] if ingress_traefik_tls["enabled"] else ["web"]
        
        # Service configuration
        ingress_traefik_service_kind = "Service"  # Standard Kubernetes service
        ingress_traefik_load_balancer_strategy = "RoundRobin"  # Default
        ingress_traefik_weighted_services = []
        ingress_traefik_mirror_services = []
        
        # Middleware configuration - extract from annotations or create defaults
        ingress_traefik_middlewares = []
        
        # Advanced options
        ingress_traefik_pass_host_header = True
        ingress_traefik_servers_transport_name = None
        ingress_traefik_timeout = None

        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')
        
        # Format entry_points as string with "- " prefix for each entry point
        entry_points_formatted = "\n".join([f"- {ep}" for ep in ingress_traefik_entry_points]) if ingress_traefik_entry_points else "- web"
        
        formatted_user_query = TRAEFIK_INGRESSROUTE_USER_PROMPT.format(
            app_name=ingress_traefik_app_name,
            route_type=ingress_traefik_route_type,
            namespace=ingress_traefik_namespace,
            traefik_rule_syntax="\n".join([f"- {route}" for route in ingress_traefik_routes]) if ingress_traefik_routes else "- Host(`example.com`)",
            Services_description="\n".join([f"- {json.dumps(service)}" for service in ingress_traefik_services]) if ingress_traefik_services else "- {}",
            entry_points=entry_points_formatted,
            service_kind=ingress_traefik_service_kind,
            load_balancer_strategy=ingress_traefik_load_balancer_strategy,
            weighted_services="\n".join([f"- {json.dumps(service)}" for service in ingress_traefik_weighted_services]) if ingress_traefik_weighted_services else "- None",
            mirror_services="\n".join([f"- {json.dumps(service)}" for service in ingress_traefik_mirror_services]) if ingress_traefik_mirror_services else "- None",
            main_service_for_mirror=json.dumps(ingress_traefik_services[0]) if ingress_traefik_services else "{}",
            middlewares_description="\n".join([f"- {middleware.get('type', 'custom')} (custom) - {json.dumps(middleware)}" for middleware in ingress_traefik_middlewares]) if ingress_traefik_middlewares else "- None",
            tls_enabled=ingress_traefik_tls.get("enabled", False),
            tls_secret_name=ingress_traefik_tls.get("secret_name", "") or "None",
            tls_cert_resolver=ingress_traefik_tls.get("cert_resolver", "") or "None",
            tls_passthrough=ingress_traefik_tls.get("passthrough", False),
            pass_host_header=ingress_traefik_pass_host_header,
            servers_transport_name=ingress_traefik_servers_transport_name or "None",
            timeout=ingress_traefik_timeout or "None",
        )

        parser = PydanticOutputParser(return_id=True, pydantic_object=TraefikIngressRouteToolOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the TraefikIngressRouteToolOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )

        config = Config()
        llm_config = config.get_llm_config()
        traefik_ingressroute_tool_logger.log_structured(
            level="INFO",
            message="Generating Traefik IngressRoute YAML file",
            extra={
                "app_name": ingress_traefik_app_name,
                "route_type": ingress_traefik_route_type,
                "namespace": ingress_traefik_namespace,
                "routes_count": len(ingress_traefik_routes),
                "services_count": len(ingress_traefik_services),
                "entry_points": ingress_traefik_entry_points,
                "tls_enabled": ingress_traefik_tls.get("enabled", False),
                "tls_cert_resolver": ingress_traefik_tls.get("cert_resolver") or None,
            }
        )
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens'],
        )
        chain = prompt | model | parser
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=TRAEFIK_INGRESSROUTE_SYSTEM_PROMPT)]
        })
        traefik_ingressroute_tool_logger.log_structured(
            level="INFO",
            message="Traefik IngressRoute YAML file generated successfully",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id,
            }
        )
        response_json = response.model_dump()
        traefik_ingressroute_yaml = response_json.get("yaml_content", "")
        file_name = response_json.get("file_name", "")
        template_variables_used = response_json.get("template_variables_used", [])


        tool_message = ToolMessage(
            content="Traefik IngressRoute YAML file generated successfully.",
            tool_call_id=tool_call_id
        )
        
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_traefik_ingressroute_yaml" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_traefik_ingressroute_yaml"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_traefik_ingressroute_yaml": {
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
        generation_metadata["quality_scores"]["generate_traefik_ingressroute_yaml"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(update={
            "messages": [tool_message],
            "generated_templates": {file_name: traefik_ingressroute_yaml},
            "template_variables": template_variables_used,
            # State tracking updates
            "completed_tools": completed_tools,
            "tool_results": tool_results,
            "generation_metadata": generation_metadata,
            "coordinator_state": coordinator_state,
            "next_action": "coordinator"
        })
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
        
        traefik_ingressroute_tool_logger.log_structured(
            level="ERROR",
            message=f"Error generating Traefik IngressRoute YAML file: {e}. Please re-run the tool.",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e