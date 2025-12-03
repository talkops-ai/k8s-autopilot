from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Literal
import uuid
import asyncio
from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger, log_sync
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.agents.base_agent import BaseSubgraphAgent
from k8s_autopilot.core.agents.helm_generator.template.tools import (
    generate_deployment_yaml,
    generate_service_yaml,
    generate_values_yaml,
    generate_helpers_tpl,
    generate_hpa_yaml,
    generate_traefik_ingressroute_yaml,
    generate_pdb_yaml,
    generate_configmap_yaml,
    generate_network_policy_yaml,
    generate_readme,
    generate_service_account_rbac,
    generate_secret,
)
from k8s_autopilot.core.agents.helm_generator.template.template_prompts import (
    COORDINATOR_SYSTEM_PROMPT,
    TOOL_EXECUTOR_SYSTEM_PROMPT
)

template_supervisor_logger = AgentLogger("k8sAutopilotTmplSupervisorAgent")

# Tool Mapping
TOOL_MAPPING = {
    "generate_deployment_yaml": generate_deployment_yaml,
    "generate_service_yaml": generate_service_yaml,
    "generate_values_yaml": generate_values_yaml,
    "generate_helpers_tpl": generate_helpers_tpl,
    "generate_hpa_yaml": generate_hpa_yaml,
    "generate_traefik_ingressroute_yaml": generate_traefik_ingressroute_yaml,
    "generate_pdb_yaml": generate_pdb_yaml,
    "generate_configmap_yaml": generate_configmap_yaml,
    "generate_network_policy_yaml": generate_network_policy_yaml,
    "generate_readme": generate_readme,
    "generate_service_account_rbac": generate_service_account_rbac,
    "generate_secret": generate_secret,
}

class MockToolRuntime:
    """Mock runtime to inject state into tools manually."""
    def __init__(self, state):
        self.state = state

class TemplateSupervisor(BaseSubgraphAgent):
    """
    Template supervisor agent that is responsible for generating the templates for the Helm chart.
    """
    def __init__(
        self, config: Optional[Config] = None, 
        custom_config: Optional[Dict[str, Any]] = None, 
        name: str = "template_supervisor_agent", 
        memory: Optional[MemorySaver] = None
        ):
        """
        Initialize the TemplateSupervisor.
        """
        template_supervisor_logger.log_structured(
            level="INFO",
            message="Initializing TemplateSupervisor",
            extra={"config": config, "custom_config": custom_config, "name": name, "memory": memory})
        
        self.config_instance = config or Config(custom_config or {})
        self._name = name
        self.memory = memory or MemorySaver()
        
        # No sub-agents needed as we use tools directly in nodes
        self._sub_agents = [] 
        
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def state_model(self) -> type[GenerationSwarmState]:
        return GenerationSwarmState
    
    def build_graph(self) -> StateGraph:
        """
        Build complete LangGraph for Helm chart generation
        """
        template_supervisor_logger.log_structured(
            level="INFO",
            message="Building graph for template supervisor",
            extra={"agent_name": self._name}
        )

        graph = StateGraph(GenerationSwarmState)
        
        # ============================================================
        # NODES
        # ============================================================
        
        # Initialization (replaces Planner Node since planning is done)
        graph.add_node("initialization", self.initialization_node)
        
        # Coordinator (routing)
        graph.add_node("coordinator", self.coordinator_node)
        
        # Tool executor
        graph.add_node("tool_executor", self.tool_executor_node)
        
        # Aggregator
        graph.add_node("aggregator", self.aggregator_node)
        
        # Error handler
        graph.add_node("error_handler", self.error_handler_node)
        
        # ============================================================
        # EDGES
        # ============================================================
        
        # Start -> Initialization
        graph.add_edge(START, "initialization")
        
        # Initialization -> Coordinator
        graph.add_edge("initialization", "coordinator")
        
        # Coordinator -> Tool Executor / Aggregator / Error Handler
        graph.add_conditional_edges(
            "coordinator",
            self.route_from_coordinator,
            {
                "tool_executor": "tool_executor",
                "aggregator": "aggregator",
                "error_handler": "error_handler"
            }
        )
        
        # Tool Executor -> Coordinator
        graph.add_edge("tool_executor", "coordinator")
        
        # Aggregator -> END
        graph.add_edge("aggregator", END)
        
        # Error Handler -> Coordinator / END
        graph.add_conditional_edges(
            "error_handler",
            self.route_from_error_handler,
            {
                "coordinator": "coordinator",
                END: END
            }
        )
        
        return graph.compile(checkpointer=self.memory)

    # ============================================================
    # NODE IMPLEMENTATIONS
    # ============================================================

    def initialization_node(self, state: GenerationSwarmState) -> Dict[str, Any]:
        """
        Setup execution state based on existing planner_output.
        """
        planner_output = state.get("planner_output", {})
        k8s_arch = planner_output.get("kubernetes_architecture", {})
        resources = k8s_arch.get("resources", {})
        auxiliary_resources = resources.get("auxiliary", [])
        
        # Map auxiliary resources to tools
        # Resource Type -> Tool Name
        RESOURCE_TO_TOOL = {
            "HorizontalPodAutoscaler": "generate_hpa_yaml",
            "PodDisruptionBudget": "generate_pdb_yaml",
            "NetworkPolicy": "generate_network_policy_yaml",
            "Ingress": "generate_traefik_ingressroute_yaml", # Only Traefik supported
            "ConfigMap": "generate_configmap_yaml",
            "Secret": "generate_secret",
            "ServiceAccount": "generate_service_account_rbac"
        }
        
        conditional_tools = []
        for resource in auxiliary_resources:
            res_type = resource.get("type")
            if res_type in RESOURCE_TO_TOOL:
                tool_name = RESOURCE_TO_TOOL[res_type]
                # Check if we actually have this tool implemented/mapped
                if tool_name in TOOL_MAPPING:
                    conditional_tools.append(tool_name)
        
        # Deduplicate
        conditional_tools = list(set(conditional_tools))
        
        # Create Tool Queue
        # Phase 1 tools (always run) - ORDER MATTERS: helpers first, then deployment/service
        # Note: values_yaml runs AFTER all templates (including conditional) but BEFORE README
        core_tools = [
            "generate_helpers_tpl",  # First - other templates use helper functions
            "generate_deployment_yaml",
            "generate_service_yaml"
            # values_yaml is NOT in core_tools - it runs after conditional templates
        ]
        
        # Phase 2: values.yaml (after all templates, before README)
        values_tools = ["generate_values_yaml"]
        
        # Phase 3 tools (documentation)
        doc_tools = ["generate_readme"]
        
        tools_to_execute = core_tools + conditional_tools + values_tools + doc_tools
        
        # Identify dependencies
        # Pass all tools to properly set README dependencies
        pending_dependencies = self.identify_tool_dependencies(conditional_tools, core_tools)
        
        return {
            "current_phase": "CORE_TEMPLATES",
            "next_action": "execute_tool",
            "tools_to_execute": tools_to_execute,
            "completed_tools": [],
            "pending_dependencies": pending_dependencies,
            "coordinator_state": {
                "phases_completed": ["PLANNING"],
                "current_retry_count": 0,
                "max_retries": 3
            },
            "generated_templates": {},
            "tool_results": {},
            "errors": []
        }

    def coordinator_node(self, state: GenerationSwarmState) -> Dict[str, Any]:
        """
        Routes work and manages state transitions.
        """
        current_phase = state.get("current_phase", "CORE_TEMPLATES")
        completed_tools = state.get("completed_tools", [])
        tools_to_execute = state.get("tools_to_execute", [])
        pending_dependencies = state.get("pending_dependencies", {})
        
        template_supervisor_logger.log_structured(
            level="DEBUG",
            message=f"Coordinator invoked",
            extra={
                "current_phase": current_phase,
                "completed_tools": completed_tools,
                "tools_to_execute_count": len(tools_to_execute)
            }
        )
        
        # PHASE: CORE_TEMPLATES
        if current_phase == "CORE_TEMPLATES":
            # Core tools - ORDER MATTERS: helpers first (other templates use helper functions)
            if "generate_helpers_tpl" not in completed_tools:
                return {"next_action": "generate_helpers_tpl"}
            
            elif "generate_deployment_yaml" not in completed_tools:
                return {"next_action": "generate_deployment_yaml"}
            
            elif "generate_service_yaml" not in completed_tools:
                return {"next_action": "generate_service_yaml"}
            
            else:
                # All core templates done (helpers, deployment, service)
                # values_yaml will run after conditional templates
                return {
                    "current_phase": "CONDITIONAL_TEMPLATES",
                    "next_action": "check_conditional_tools",
                    "coordinator_state": {
                        **state.get("coordinator_state", {}),
                        "phases_completed": state.get("coordinator_state", {}).get("phases_completed", []) + ["CORE_TEMPLATES"]
                    }
                }
        
        # PHASE: CONDITIONAL_TEMPLATES
        elif current_phase == "CONDITIONAL_TEMPLATES":
            # Find next conditional tool to execute
            for tool in tools_to_execute:
                if tool not in completed_tools and tool not in ["generate_readme", "generate_deployment_yaml", "generate_service_yaml", "generate_values_yaml", "generate_helpers_tpl"]:
                    # Check if dependencies are met
                    deps = pending_dependencies.get(tool, [])
                    if all(dep in completed_tools for dep in deps):
                        return {"next_action": tool}
            
            # All conditional tools done (or none needed)
            # Now generate values.yaml (after all templates, before README)
            if "generate_values_yaml" not in completed_tools:
                return {
                    "next_action": "generate_values_yaml",
                    "coordinator_state": {
                        **state.get("coordinator_state", {}),
                        "phases_completed": state.get("coordinator_state", {}).get("phases_completed", []) + ["CONDITIONAL_TEMPLATES"]
                    }
                }
            else:
                # values.yaml done, move to documentation
                return {
                    "current_phase": "DOCUMENTATION",
                    "next_action": "generate_readme",
                    "coordinator_state": {
                        **state.get("coordinator_state", {}),
                        "phases_completed": state.get("coordinator_state", {}).get("phases_completed", []) + ["CONDITIONAL_TEMPLATES"]
                    }
                }
        
        # PHASE: DOCUMENTATION
        elif current_phase == "DOCUMENTATION":
            if "generate_readme" not in completed_tools:
                return {"next_action": "generate_readme"}
            else:
                return {
                    "current_phase": "AGGREGATION",
                    "next_action": "aggregate_chart",
                    "coordinator_state": {
                        **state.get("coordinator_state", {}),
                        "phases_completed": state.get("coordinator_state", {}).get("phases_completed", []) + ["DOCUMENTATION"]
                    }
                }
        
        # PHASE: AGGREGATION
        elif current_phase == "AGGREGATION":
            return {
                "next_action": "aggregation",
                "coordinator_state": {
                    **state.get("coordinator_state", {}),
                    "final_status": "SUCCESS"
                }
            }
        
        # PHASE: COMPLETED
        elif current_phase == "COMPLETED":
            return {"final_status": "SUCCESS"}
            
        return {}

    async def tool_executor_node(self, state: GenerationSwarmState) -> Any:
        """
        Executes a single tool based on next_action.
        """
        tool_name = state.get("next_action")
        completed_tools = state.get("completed_tools", [])
        
        template_supervisor_logger.log_structured(
            level="DEBUG",
            message=f"Tool executor invoked",
            extra={
                "tool_name": tool_name,
                "completed_tools": completed_tools,
                "current_phase": state.get("current_phase")
            }
        )
        
        # If next_action is not a valid tool name, this shouldn't happen
        # The coordinator should only route here with valid tool names
        if not tool_name or tool_name in ["check_conditional_tools", "execute_tool", "coordinator", "aggregate_chart", "aggregation"]:
            # This is a routing error - log and return empty to let coordinator decide
            template_supervisor_logger.log_structured(
                level="WARNING",
                message=f"Tool executor called with invalid action: {tool_name}",
                extra={"next_action": tool_name}
            )
            return {}
            
        try:
            # Execute Tool
            tool_func = TOOL_MAPPING.get(tool_name)
            if not tool_func:
                raise ValueError(f"Tool {tool_name} not found in mapping")
            
            template_supervisor_logger.log_structured(
                level="DEBUG",
                message=f"Found tool function",
                extra={
                    "tool_name": tool_name,
                    "tool_type": type(tool_func).__name__,
                    "has_wrapped": hasattr(tool_func, '__wrapped__'),
                    "has_func": hasattr(tool_func, 'func'),
                    "has_coroutine": hasattr(tool_func, 'coroutine')
                }
            )
                
            # Create Mock Runtime to inject state
            mock_runtime = MockToolRuntime(state)
            tool_call_id = f"call_{tool_name}_{uuid.uuid4().hex[:8]}"
            
            template_supervisor_logger.log_structured(
                level="INFO",
                message=f"Invoking tool: {tool_name}",
                extra={"tool_call_id": tool_call_id}
            )
            
            # Call the underlying coroutine function directly
            # For async tools, the coroutine is stored in .coroutine
            # For sync tools, it's in .func
            # For decorated tools, it might be in __wrapped__
            
            if hasattr(tool_func, '__wrapped__'):
                underlying_func = tool_func.__wrapped__
            elif hasattr(tool_func, 'coroutine') and tool_func.coroutine is not None:
                underlying_func = tool_func.coroutine
            else:
                underlying_func = tool_func.func
            
            template_supervisor_logger.log_structured(
                level="DEBUG",
                message=f"Calling underlying function",
                extra={
                    "underlying_func_type": type(underlying_func).__name__,
                    "is_coroutine_function": asyncio.iscoroutinefunction(underlying_func)
                }
            )
            
            if underlying_func is None:
                raise ValueError(f"Could not find underlying function for tool {tool_name}")

            result_command = await underlying_func(runtime=mock_runtime, tool_call_id=tool_call_id)
            
            template_supervisor_logger.log_structured(
                level="INFO",
                message=f"Tool execution completed: {tool_name}",
                extra={
                    "result_type": type(result_command).__name__,
                    "has_update": hasattr(result_command, 'update')
                }
            )
            
            # We also need to update the coordinator state to mark this tool as completed
            # The tool's Command updates 'generated_templates' etc., but we need to track 'completed_tools'
            # We can merge this into the returned Command if possible, or return a dict that LangGraph merges.
            # However, returning a Command usually overrides. 
            # Let's inspect the Command. If it's a Command object, we can't easily "add" to it without accessing its internals.
            # BUT, LangGraph allows returning a Command.
            
            # Strategy: The tool updates the artifacts. The coordinator needs to know it's done.
            # We can append the 'completed_tools' update to the Command's update dict if it's accessible,
            # or we rely on the tool to update it (which it doesn't know about).
            
            # Better Strategy: 
            # The tool returns a Command. We can wrap it or modify it.
            # Since Command is a class, let's assume we can read its 'update' field.
            
            # Calculate completed_tools safely (avoiding duplicates)
            current_completed = state.get("completed_tools", [])
            if tool_name not in current_completed:
                completed_tools = current_completed + [tool_name]
            else:
                completed_tools = current_completed
            
            if hasattr(result_command, 'update') and isinstance(result_command.update, dict):
                 # Merge our coordinator updates into the tool's updates ONLY if missing
                 # This allows "smart tools" to handle their own state logic
                 if "completed_tools" not in result_command.update:
                     result_command.update["completed_tools"] = completed_tools
                 
                 if "coordinator_state" not in result_command.update:
                     result_command.update["coordinator_state"] = {
                        **state.get("coordinator_state", {}),
                        "current_retry_count": 0
                    }
                 return result_command
            else:
                # Fallback if it's not a Command object or doesn't have update dict
                return {
                    "completed_tools": completed_tools,
                    # Don't set next_action - let coordinator decide
                    "coordinator_state": {
                        **state.get("coordinator_state", {}),
                        "current_retry_count": 0
                    }
                }
            
        except Exception as e:
            # Error Handling
            retry_count = state.get("coordinator_state", {}).get("current_retry_count", 0)
            
            error_entry = {
                "tool": tool_name,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
                "retry_count": retry_count
            }
            
            updated_errors = state.get("errors", []) + [error_entry]
            
            return {
                "errors": updated_errors,
                "next_action": "error_handler",
                "coordinator_state": {
                    **state.get("coordinator_state", {}),
                    "error_details": str(e)
                }
            }

    def aggregator_node(self, state: GenerationSwarmState) -> Dict[str, Any]:
        """
        Assembles final Helm chart.
        """
        generated_templates = state.get("generated_templates", {})
        planner_output = state.get("planner_output", {})
        app_analysis = planner_output.get("application_analysis", {})
        
        # Create Chart.yaml
        chart_yaml = self.generate_chart_yaml(
            app_name=app_analysis.get("app_name", "my-app"),
            app_description=app_analysis.get("description", "Helm chart generated by Autopilot"),
            version="1.0.0"
        )
        
        # Organize File Structure
        final_helm_chart = {
            "Chart.yaml": chart_yaml,
        }
        
        for filename, content in generated_templates.items():
            if filename not in ["Chart.yaml", "values.yaml", "README.md"]:
                if filename.endswith(".yaml") and not filename.startswith("_"):
                    final_helm_chart[f"templates/{filename}"] = content
                elif filename == "_helpers.tpl":
                    final_helm_chart["templates/_helpers.tpl"] = content
            elif filename == "values.yaml":
                final_helm_chart["values.yaml"] = content
            elif filename == "README.md":
                final_helm_chart["README.md"] = content
                
        return {
            "final_helm_chart": final_helm_chart,
            "final_status": "SUCCESS",
            "current_phase": "COMPLETED",
            "coordinator_state": {
                **state.get("coordinator_state", {}),
                "phases_completed": state.get("coordinator_state", {}).get("phases_completed", []) + ["AGGREGATION"],
                "final_status": "SUCCESS"
            }
        }

    def error_handler_node(self, state: GenerationSwarmState) -> Dict[str, Any]:
        """
        Handle and recover from errors.
        """
        errors = state.get("errors", [])
        if not errors:
            return {"next_action": "coordinator"}
            
        latest_error = errors[-1]
        retry_count = state.get("coordinator_state", {}).get("current_retry_count", 0)
        max_retries = state.get("coordinator_state", {}).get("max_retries", 3)
        
        # Simple retry logic
        if retry_count < max_retries:
            return {
                "coordinator_state": {
                    **state.get("coordinator_state", {}),
                    "current_retry_count": retry_count + 1
                },
                "next_action": latest_error["tool"] # Retry same tool
            }
        else:
            return {
                "final_status": "FAILED",
                "coordinator_state": {
                    **state.get("coordinator_state", {}),
                    "final_status": "FAILED",
                    "error_details": f"Max retries exceeded for {latest_error['tool']}"
                }
            }

    # ============================================================
    # HELPER METHODS
    # ============================================================

    def route_from_coordinator(self, state: GenerationSwarmState) -> str:
        next_action = state.get("next_action")
        
        # Route to aggregator
        if next_action == "aggregate_chart" or next_action == "aggregation":
            return "aggregator"
        
        # Route to error handler
        elif next_action == "error_handler":
            return "error_handler"
        
        # All other actions should be tool names - route to tool executor
        else:
            return "tool_executor"

    def route_from_error_handler(self, state: GenerationSwarmState) -> str:
        if state.get("coordinator_state", {}).get("final_status") == "FAILED":
            return END
        else:
            return "coordinator"

    def identify_tool_dependencies(self, conditional_tools: List[str], core_tools: List[str]) -> Dict[str, List[str]]:
        """
        Identify which tools depend on which other tools.
        
        Args:
            conditional_tools: List of conditional tools to execute
            core_tools: List of core tools (always executed)
        
        Returns:
            Dict mapping tool_name -> list of dependency tool names
        """
        dependencies = {}
        
        # HPA requires Deployment
        if "generate_hpa_yaml" in conditional_tools:
            dependencies["generate_hpa_yaml"] = ["generate_deployment_yaml"]
        
        # PDB requires Deployment
        if "generate_pdb_yaml" in conditional_tools:
            dependencies["generate_pdb_yaml"] = ["generate_deployment_yaml"]
        
        # NetworkPolicy requires Deployment
        if "generate_network_policy_yaml" in conditional_tools:
            dependencies["generate_network_policy_yaml"] = ["generate_deployment_yaml"]
        
        # Ingress/Traefik require Service AND helpers (for template functions)
        ingress_tools = ["generate_traefik_ingressroute_yaml"]
        for tool in ingress_tools:
            if tool in conditional_tools:
                dependencies[tool] = ["generate_service_yaml", "generate_helpers_tpl"]
        
        # ConfigMap and Secret can run independently (no hard dependencies)
        # ServiceAccount/RBAC can run independently
        
        # values.yaml should run after all templates (core + conditional) but before README
        # It depends on all templates to consolidate all configuration values
        all_template_tools = core_tools + conditional_tools
        dependencies["generate_values_yaml"] = all_template_tools
        
        # README requires ALL generated templates (core + conditional) AND values.yaml
        # This ensures README is generated last with complete information including values
        dependencies["generate_readme"] = all_template_tools + ["generate_values_yaml"]
        
        return dependencies

    def generate_chart_yaml(self, app_name: str, app_description: str, version: str) -> str:
        return f"""apiVersion: v2
kind: Chart
metadata:
  name: {app_name}
  description: {app_description}
  type: application
  version: {version}
  appVersion: "1.0.0"
"""


@log_sync
def create_template_supervisor(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "template_supervisor",
    memory: Optional[MemorySaver] = None
) -> TemplateSupervisor:
    return TemplateSupervisor(config=config, custom_config=custom_config, name=name, memory=memory)

def create_template_supervisor_factory(config: Config):
    return create_template_supervisor(config=config)