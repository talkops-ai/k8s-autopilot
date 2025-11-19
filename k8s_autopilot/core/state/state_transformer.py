from typing import Dict
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.state.base import (
    MainSupervisorState,
    PlanningSwarmState,
    GenerationSwarmState,
    ValidationSwarmState,
    WorkflowStatus
)


class StateTransformer:
    """
    Handles state transformations between supervisor and swarm subgraphs.
    
    Each transformation:
    1. Transforms parent (supervisor) state to subgraph (swarm) state
    2. Invokes subgraph
    3. Transforms subgraph results back to parent state
    
    TypedDict Note: Use dict literals, not constructors!
    """

    # ============================================================================
    # Planning Swarm Transformations
    # ============================================================================
    
    @staticmethod
    def supervisor_to_planning(supervisor_state: MainSupervisorState) -> Dict:
        """
        Transform supervisor state to planning swarm input state.
        
        Maps:
        - user_requirements → requirements (ChartRequirements)
        - messages → messages (conversation history)
        - Creates empty handoff_metadata dict
        """
        messages = [HumanMessage(content=supervisor_state["user_query"])]
        # Get status and convert to string value to avoid Pydantic serialization warnings
        # PlanningSwarmState.status now accepts str | WorkflowStatus
        status = supervisor_state.get("status", WorkflowStatus.PENDING)
        if isinstance(status, WorkflowStatus):
            status_value = status.value  # Convert enum to string
        else:
            status_value = str(status)  # Ensure it's a string
        
        return {
            "messages": messages,
            "remaining_steps": None,  # Required by Deep Agent TodoListMiddleware
            "active_agent": "requirement_analyzer",  # Start with supervisor
            "chart_plan": None,
            "status": status_value,  # Use string value to avoid serialization warnings
            "todos": [],
            "workspace_files": {},
            "handoff_metadata": {},
            "session_id": supervisor_state.get("session_id"),
            "task_id": supervisor_state.get("task_id"),
            "user_query": supervisor_state.get("user_query")
        }
    
    @staticmethod
    def planning_to_supervisor(
        planning_state: PlanningSwarmState,
        original_supervisor_state: MainSupervisorState
    ) -> Dict:
        """
        Transform planning swarm output back to supervisor state updates.
        
        Maps:
        - chart_plan → planning_output (ChartPlan)
        - messages → messages (updated conversation)
        - Updates workflow_state.planning_complete
        """
        return {
            "messages": planning_state["messages"],
            "planning_output": planning_state.get("chart_plan"),
            "workflow_state": {
                **original_supervisor_state.get("workflow_state", {}).__dict__,
                "planning_complete": True,
                "last_swarm": "planning_swarm",
                "current_phase": "planning"
            }
        }
    
    # ============================================================================
    # Generation Swarm Transformations
    # ============================================================================
    
    @staticmethod
    def supervisor_to_generation(supervisor_state: MainSupervisorState) -> Dict:
        """
        Transform supervisor state to generation swarm input state.
        
        Maps:
        - planning_output → chart_plan (ChartPlan from planning phase)
        - messages → messages (conversation history)
        """
        return {
            "messages": supervisor_state["messages"],
            "active_agent": "generation_supervisor",
            "chart_plan": supervisor_state.get("planning_output"),
            "templates": {},
            "values_yaml": None,
            "values_schema_json": None,
            "chart_yaml": None,
            "readme": None,
            "security_policies": [],
            "todos": [],
            "workspace_files": {},
            "generation_metadata": {},
            "handoff_metadata": {}
        }
    
    @staticmethod
    def generation_to_supervisor(
        generation_state: GenerationSwarmState,
        original_supervisor_state: MainSupervisorState
    ) -> Dict:
        """
        Transform generation swarm output back to supervisor state updates.
        
        Maps:
        - templates + values_yaml + chart_yaml + readme → generated_artifacts (Dict[str, str])
        - messages → messages (updated conversation)
        - Updates workflow_state.generation_complete
        """
        # Combine all generated files into single dict
        generated_artifacts = {}
        
        # Add templates
        if generation_state.get("templates"):
            generated_artifacts.update(generation_state["templates"])
        
        # Add standalone files
        if generation_state.get("values_yaml"):
            generated_artifacts["values.yaml"] = generation_state["values_yaml"]
        if generation_state.get("values_schema_json"):
            generated_artifacts["values.schema.json"] = generation_state["values_schema_json"]
        if generation_state.get("chart_yaml"):
            generated_artifacts["Chart.yaml"] = generation_state["chart_yaml"]
        if generation_state.get("readme"):
            generated_artifacts["README.md"] = generation_state["readme"]
        
        return {
            "messages": generation_state["messages"],
            "generated_artifacts": generated_artifacts if generated_artifacts else None,
            "workflow_state": {
                **original_supervisor_state.get("workflow_state", {}).__dict__,
                "generation_complete": True,
                "last_swarm": "generation_swarm",
                "current_phase": "generation"
            }
        }
    
    # ============================================================================
    # Validation Swarm Transformations
    # ============================================================================
    
    @staticmethod
    def supervisor_to_validation(supervisor_state: MainSupervisorState) -> Dict:
        """
        Transform supervisor state to validation swarm input state.
        
        Maps:
        - generated_artifacts → generated_chart (Dict[str, str])
        - planning_output → chart_metadata (ChartPlan)
        - messages → messages (conversation history)
        """
        return {
            "messages": supervisor_state["messages"],
            "active_agent": "validation_supervisor",
            "generated_chart": supervisor_state.get("generated_artifacts", {}),
            "chart_metadata": supervisor_state.get("planning_output"),
            "validation_results": [],
            "security_scan_results": None,
            "test_artifacts": None,
            "argocd_manifests": None,
            "deployment_ready": False,
            "blocking_issues": [],
            "handoff_metadata": {}
        }
    
    @staticmethod
    def validation_to_supervisor(
        validation_state: ValidationSwarmState,
        original_supervisor_state: MainSupervisorState
    ) -> Dict:
        """
        Transform validation swarm output back to supervisor state updates.
        
        Maps:
        - validation_results → validation_results (List[ValidationResult])
        - messages → messages (updated conversation)
        - Updates workflow_state.validation_complete
        """
        return {
            "messages": validation_state["messages"],
            "validation_results": validation_state.get("validation_results", []),
            "workflow_state": {
                **original_supervisor_state.get("workflow_state", {}).__dict__,
                "validation_complete": True,
                "last_swarm": "validation_swarm",
                "current_phase": "validation",
                "deployment_ready": validation_state.get("deployment_ready", False)
            }
        }