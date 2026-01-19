from typing import Dict
from langchain_core.messages import HumanMessage, ToolMessage
from k8s_autopilot.core.state.base import (
    MainSupervisorState,
    PlanningSwarmState,
    GenerationSwarmState,
    ValidationSwarmState,
    WorkflowStatus,
    SupervisorWorkflowState,
    HelmAgentState
)
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for StateTransformer
state_transformer_logger = AgentLogger("StateTransformer")


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
        
        # Preserve existing plan/requirements if available to prevent loop
        existing_plan = supervisor_state.get("planner_output")

        return {
            "messages": messages,
            "remaining_steps": None,  # Required by Deep Agent TodoListMiddleware
            "active_agent": "requirement_analyzer",  # Start with supervisor
            "chart_plan": existing_plan,
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
        original_supervisor_state: MainSupervisorState,
        tool_call_id: str
    ) -> Dict:
        """
        Transform planning swarm output back to supervisor state updates.
        
        Maps:
        - chart_plan → planning_output (ChartPlan)
        - messages → messages (updated conversation)
        - Updates workflow_state.planning_complete
        """
        # Reconstruct workflow state object to use helper methods
        current_workflow_state = original_supervisor_state.get("workflow_state")
        if current_workflow_state:
            # Ensure it's a SupervisorWorkflowState object
            if isinstance(current_workflow_state, dict):
                workflow_state_obj = SupervisorWorkflowState(**current_workflow_state)
            else:
                workflow_state_obj = current_workflow_state
            
            # Use helper method to set phase complete
            workflow_state_obj.set_phase_complete("planning")
            workflow_state_obj.last_swarm = "planning_swarm"
            
            # Return object directly to avoid Pydantic serialization warnings
            updated_workflow_state = workflow_state_obj
        else:
            # Fallback if no workflow state exists
            updated_workflow_state = {
                "planning_complete": True,
                "last_swarm": "planning_swarm",
                "current_phase": "planning"
            }

        # Extract planning output (chart_plan)
        planning_output = planning_state.get("chart_plan") or planning_state.get("handoff_data")
        
        # Create summary of the plan for the LLM context
        try:
             # Convert to dict for summary extraction
            if isinstance(planning_output, dict):
                plan_dict = planning_output
            else:
                # Handle Pydantic model (ChartPlan)
                plan_dict = planning_output.model_dump() if hasattr(planning_output, 'model_dump') else dict(planning_output)
            
            from k8s_autopilot.core.hitl.utils import extract_planning_summary
            plan_summary_text = extract_planning_summary(plan_dict)
        except Exception:
            plan_summary_text = "Chart plan details stored in state."

        # Create summary messages instead of dumping full history
        summary_messages = [
            ToolMessage(
                content=f"Planning swarm completed successfully. Chart plan summary:\n{plan_summary_text}\n\nPlease transfer call to transfer_to_template_supervisor tool.",
                tool_call_id=tool_call_id
            ),
            HumanMessage(
                content="Planning is complete. Please proceed to generate the Helm chart artifacts based on the plan."
            )
        ]

        return {
            "messages": summary_messages,
            "llm_input_messages": summary_messages,
            "planner_output": planning_output,
            "workflow_state": updated_workflow_state
        }
    
    # ============================================================================
    # Generation Swarm Transformations
    # ============================================================================
    
    @staticmethod
    def supervisor_to_generation(supervisor_state: MainSupervisorState) -> Dict:
        """
        Transform supervisor state to generation swarm input state.
        
        Maps:
        - planner_output → planner_output (ChartPlan from planning phase)
        - messages → messages (conversation history)
        
        Note: The Template Coordinator's initialization_node will set up:
        - current_phase, next_action, tools_to_execute, completed_tools
        - pending_dependencies, coordinator_state, tool_results, errors
        based on the planner_output, so we don't set them here.
        """
        messages = [HumanMessage(content=supervisor_state["user_query"])]
        
        # Prepare workflow state with generation phase
        workflow_state = supervisor_state.get("workflow_state")
        if workflow_state:
            # Ensure it's a SupervisorWorkflowState object
            if isinstance(workflow_state, dict):
                workflow_state_obj = SupervisorWorkflowState(**workflow_state)
            else:
                # Copy or use existing object
                workflow_state_obj = workflow_state
            
            # Update phase
            workflow_state_obj.current_phase = "generation"
            workflow_state_obj.next_swarm = "template_supervisor"
        else:
             # Fallback if no workflow state exists
            workflow_state_obj = {
                "current_phase": "generation",
                "next_swarm": "template_supervisor"
            }

        return {
            # Core fields
            "messages": messages,
            "planner_output": supervisor_state.get("planner_output"),
            "workflow_state": workflow_state_obj, # Pass workflow state to generation swarm
            
            # Generated artifacts (will be populated by tools)
            "generated_templates": {},
            "validation_results": [],
            "template_variables": [],
            "session_id": supervisor_state.get("session_id"),
            "task_id": supervisor_state.get("task_id"),
            # Generation status (legacy field, kept for compatibility)
            "generation_status": {},
        }
    
    @staticmethod
    def generation_to_supervisor(
        generation_state: GenerationSwarmState,
        original_supervisor_state: MainSupervisorState,
        tool_call_id: str
    ) -> Dict:
        """
        Transform generation swarm output back to supervisor state updates.
        
        Maps:
        - templates + values_yaml + chart_yaml + readme → generated_artifacts (Dict[str, str])
        - messages → messages (updated conversation)
        - Updates workflow_state.generation_complete
        """
        current_workflow_state = original_supervisor_state.get("workflow_state")
        if current_workflow_state:
            # Ensure it's a SupervisorWorkflowState object
            if isinstance(current_workflow_state, dict):
                workflow_state_obj = SupervisorWorkflowState(**current_workflow_state)
            else:
                workflow_state_obj = current_workflow_state
            
            # Use helper method to set phase complete
            workflow_state_obj.set_phase_complete("generation")
            workflow_state_obj.last_swarm = "generation_swarm"
            
            # Return object directly to avoid Pydantic serialization warnings
            updated_workflow_state = workflow_state_obj
        else:
            # Fallback if no workflow state exists
            updated_workflow_state = {
                "generation_complete": True,
                "last_swarm": "generation_swarm",
                "current_phase": "generation"
            }

        # Create summary messages instead of dumping full history
        summary_messages = [
            ToolMessage(
                content="Generation swarm completed successfully. Chart artifacts have been generated. Please transfer call to transfer_to_validation_swarm tool.",
                tool_call_id=tool_call_id
            ),
            HumanMessage(
                content="Generation is complete. Please proceed to validate the generated artifacts."
            )
        ]

        return {
            "messages": summary_messages,
            "llm_input_messages": summary_messages,
            "helm_chart_artifacts": generation_state.get("final_helm_chart"),
            "workflow_state": updated_workflow_state
        }
    
    # ============================================================================
    # Validation Swarm Transformations
    # ============================================================================
    
    @staticmethod
    def supervisor_to_validation(supervisor_state: MainSupervisorState, workspace_dir: str = "/tmp/helm-charts") -> Dict:
        """
        Transform supervisor state to validation swarm input state.
        
        Maps:
        - generated_artifacts → generated_chart (Dict[str, str])
        - planning_output → chart_metadata (ChartPlan)
        - messages → messages (conversation history)
        
        Also pre-writes chart files to filesystem to avoid context overload.
        """
        generated_chart = supervisor_state.get("helm_chart_artifacts", {}) or {}
        
        # Extract chart name from Chart.yaml if available
        chart_name = "my-app"  # default
        if "Chart.yaml" in generated_chart:
            try:
                import yaml
                chart_metadata = yaml.safe_load(generated_chart["Chart.yaml"])
                chart_name = chart_metadata.get("name", "my-app")
            except Exception:
                pass
        
        # Pre-write chart files to filesystem to avoid context overload
        # This way the agent can work with files directly without needing them in messages
        chart_path = f"{workspace_dir}/{chart_name}"
        files_written = []
        if generated_chart:
            import os
            try:
                # Directory-Level Check (The Better Approach)
                # If the chart directory already exists, assume it's initialized and modified.
                # Do NOT overwrite anything. This preserves agent fixes during resume loops.
                if os.path.exists(chart_path) and os.path.exists(f"{chart_path}/Chart.yaml"):
                    files_written.append("(skipped, chart directory exists)")
                    # We skip the entire writing loop
                else:
                    # Initialize: Create chart from state
                    os.makedirs(chart_path, exist_ok=True)
                    os.makedirs(f"{chart_path}/templates", exist_ok=True)
                    
                    # Write all files as-is
                    for filename, content in generated_chart.items():
                        try:
                            # Content already has actual newline characters (\n) - no decoding needed
                            if not isinstance(content, str):
                                content = str(content)
                            
                            # Determine file path and create directory structure
                            file_path = f"{chart_path}/{filename}"
                            
                            # Handle templates/ subdirectory
                            if filename.startswith("templates/"):
                                template_name = filename.replace("templates/", "")
                                file_path = f"{chart_path}/templates/{template_name}"
                            elif "/" in filename and not filename.startswith("templates/"):
                                # Handle other subdirectories
                                dir_path = f"{chart_path}/{os.path.dirname(filename)}"
                                os.makedirs(dir_path, exist_ok=True)
                                file_path = f"{chart_path}/{filename}"
                            
                            # Ensure parent directory exists
                            parent_dir = os.path.dirname(file_path)
                            if parent_dir and parent_dir != chart_path:
                                os.makedirs(parent_dir, exist_ok=True)
                            
                            # Write file with proper encoding and newline handling
                            with open(file_path, "w", encoding="utf-8", newline="") as f:
                                f.write(content)
                            files_written.append(filename)
                        except Exception as e:
                            # Log but continue
                            state_transformer_logger.log_structured(
                                level="WARNING",
                                message=f"Failed to pre-write chart file: {filename}",
                                extra={
                                    "filename": filename,
                                    "error": str(e),
                                    "error_type": type(e).__name__,
                                    "chart_path": chart_path
                                }
                            )
            except Exception as e:
                # If directory creation fails, agent will handle it
                state_transformer_logger.log_structured(
                    level="WARNING",
                    message=f"Failed to create chart directory",
                    extra={
                        "chart_path": chart_path,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "workspace_dir": workspace_dir
                    }
                )
        
        # Create minimal instruction message (no chart contents to avoid context overload)
        files_status = f"Pre-written ({len(files_written)}/{len(generated_chart)} files)" if files_written else "Need to write from state"
        instruction = f"""Validate the Helm chart located at: {chart_path}

Chart Name: {chart_name}
Chart Files: {files_status}

Steps:
1. Use `ls {chart_path}` to verify chart structure
2. If files are missing, check `generated_chart` in state and write them using `write_file`
3. Run `helm_lint_validator(chart_path="{chart_path}")`
4. Run `helm_template_validator(chart_path="{chart_path}")`
5. Run `helm_dry_run_validator(chart_path="{chart_path}", release_name="{chart_name}", namespace="default")` if cluster available
6. Report validation results

Chart files should be at: {chart_path}"""
        
        messages = [HumanMessage(content=instruction)]
        
        return {
            "messages": messages,
            "active_agent": "validation_supervisor",
            "generated_chart": generated_chart,  # Keep in state for reference, but not in messages
            "validation_results": [],
            "test_artifacts": None,
            "argocd_manifests": None,
            "session_id": supervisor_state.get("session_id"),
            "task_id": supervisor_state.get("task_id"),
            "blocking_issues": [],
            "handoff_metadata": {
                "chart_name": chart_name,
                "chart_path": chart_path,
                "workspace_dir": workspace_dir
            }
        }
    
    @staticmethod
    def validation_to_supervisor(
        validation_state: ValidationSwarmState,
        original_supervisor_state: MainSupervisorState,
        tool_call_id: str = "validation_complete"
    ) -> Dict:
        """
        Transform validation swarm output back to supervisor state updates.
        
        Maps:
        - validation_results → validation_results (List[ValidationResult])
        - blocking_issues → blocking_issues (List[str])
        - test_artifacts → test_artifacts (if present)
        - argocd_manifests → argocd_manifests (if present)
        - messages → messages (summary messages with ToolMessage and HumanMessage)
        - Updates workflow_state.validation_complete via set_phase_complete("validation")
        """
        # Reconstruct workflow state object
        current_workflow_state = original_supervisor_state.get("workflow_state")
        if current_workflow_state:
            # Ensure it's a SupervisorWorkflowState object
            if isinstance(current_workflow_state, dict):
                workflow_state_obj = SupervisorWorkflowState(**current_workflow_state)
            else:
                workflow_state_obj = current_workflow_state
            
            # Note: We do NOT set phase complete here blindly anymore.
            # It will be set at the end based on validation results.
            
            # Return object directly to avoid Pydantic serialization warnings
            updated_workflow_state = workflow_state_obj
        else:
            # Fallback if no workflow state exists
            updated_workflow_state = {
                # Do not assume complete by default
                "validation_complete": False,
                "last_swarm": "validation_swarm",
                "current_phase": "validation"
            }

        # Determine validation status summary
        validation_results = validation_state.get("validation_results", [])
        blocking_issues = validation_state.get("blocking_issues", [])
        deployment_ready = validation_state.get("deployment_ready", False)
        
        # Count validation results by status (handle both Pydantic models and dicts)
        passed_count = 0
        has_failures = False
        
        if blocking_issues:
            has_failures = True
            
        for r in validation_results:
            is_passed = False
            if hasattr(r, 'passed'):
                # Pydantic model
                is_passed = r.passed
            elif isinstance(r, dict):
                # Dict format
                is_passed = r.get("passed", False)
            
            if is_passed:
                passed_count += 1
            else:
                has_failures = True
        
        failed_count = len(validation_results) - passed_count
        
        # Build validation summary message
        # Determine if we should mark phase as complete (success)
        # We only mark it complete if no failures, OR if deployment is already ready (manual override?)
        phase_completed_successfully = not has_failures
        
        if phase_completed_successfully:
            # Use helper method to set phase complete ONLY on success
            workflow_state_obj.set_phase_complete("validation")
            validation_summary = (
                f"Validation completed successfully. All {len(validation_results)} validation checks passed. "
                f"Chart is ready for deployment."
            )
        elif blocking_issues:
            # DO NOT set phase complete
             if hasattr(workflow_state_obj, "validation_complete"):
                workflow_state_obj.validation_complete = False
             elif isinstance(workflow_state_obj, dict):
                workflow_state_obj["validation_complete"] = False
                
             validation_summary = (
                f"Validation FAILED with {len(blocking_issues)} blocking issue(s) and {failed_count} failed check(s). "
                f"Chart is NOT ready for deployment. Please FIX the issues."
            )
        else:
             # DO NOT set phase complete
             if hasattr(workflow_state_obj, "validation_complete"):
                workflow_state_obj.validation_complete = False
             elif isinstance(workflow_state_obj, dict):
                workflow_state_obj["validation_complete"] = False
                
             validation_summary = (
                f"Validation FAILED with {failed_count} failed check(s) out of {len(validation_results)} total. "
                f"Review validation results and FIX them before proceeding."
            )
        
        workflow_state_obj.last_swarm = "validation_swarm"
            
        # Return object directly to avoid Pydantic serialization warnings
        updated_workflow_state = workflow_state_obj

        # Create summary messages instead of dumping full history
        summary_messages = [
            ToolMessage(
                content=f"Validation swarm finished. {validation_summary}",
                tool_call_id=tool_call_id
            ),
            HumanMessage(
                content=(
                    f"Validation phase finished. "
                    f"Results: {passed_count} passed, {failed_count} failed. "
                    f"{'Chart is ready for deployment.' if phase_completed_successfully else 'Chart requires fixes.'}"
                )
            )
        ]

        # Build return dictionary with all validation data
        return_dict = {
            "messages": summary_messages,
            "llm_input_messages": summary_messages,
            "validation_results": validation_results,
            "workflow_state": updated_workflow_state
        }
        
        # Include optional validation outputs if present
        if validation_state.get("test_artifacts"):
            return_dict["test_artifacts"] = validation_state.get("test_artifacts")
        
        if validation_state.get("argocd_manifests"):
            return_dict["argocd_manifests"] = validation_state.get("argocd_manifests")
        
        # Include blocking issues in a structured way
        if blocking_issues:
            return_dict["blocking_issues"] = blocking_issues

        return return_dict

    # ============================================================================
    # Helm Management Agent Transformations
    # ============================================================================

    @staticmethod
    def supervisor_to_helm_mgmt(supervisor_state: MainSupervisorState) -> Dict:
        """
        Transform supervisor state to Helm Management Agent input state.
        
        Maps:
        - user_query → user_request
        - messages → messages (initialized with user query)
        - Initializes all required HelmAgentState fields with defaults
        """
        user_query = supervisor_state.get("user_query", "")
        messages = [HumanMessage(content=user_query)] if user_query else []
        
        return {
            "messages": messages,
            "user_request": user_query,
            "user_id": "user",  # Default
            "session_id": supervisor_state.get("session_id", "default"),
            "cluster_context": {},
            
            # Chart discovery phase
            "chart_metadata": {},
            "chart_search_results": [],
            
            # Values and configuration
            "user_provided_values": {},
            "merged_values": {},
            "validation_errors": [],
            "validation_status": "pending",
            
            # Planning phase
            "installation_plan": {},
            "plan_validation_results": {},
            "prerequisites_check_results": {},
            
            # Approval phase
            "approval_checkpoints": [],
            "pending_approval": False,
            "approval_status": "pending",
            
            # Execution phase
            "execution_started_at": None,
            "execution_status": "not_started",
            "execution_logs": [],
            "helm_release_name": None,
            "helm_release_namespace": None,
            
            # Monitoring and rollback
            "deployment_status": {},
            "rollback_available": False,
            "rollback_executed": False,
            
            # Error tracking
            "last_error": None,
            "error_count": 0,
            "recovery_attempts": [],
            
            # Request classification (for dual-path routing)
            "request_classification": None,
            "request_type": "unknown",
            "operation_name": "",
            
            # Query-specific fields
            "query_results": [],
            "query_formatted_response": "",
            
            # Audit trail
            "audit_log": []
        }

    @staticmethod
    def helm_mgmt_to_supervisor(
        helm_state: HelmAgentState,
        original_supervisor_state: MainSupervisorState,
        tool_call_id: str
    ) -> Dict:
        """
        Transform Helm Management Agent output back to supervisor state updates.
        
        Maps:
        - messages → summary ToolMessage with actual response content
        - Updates workflow_state.helm_mgmt_complete via set_phase_complete("helm_mgmt")
        - Stores response in helm_mgmt_response field
        """
        
        # Reconstruct workflow state object (similar to validation_to_supervisor)
        current_workflow_state = original_supervisor_state.get("workflow_state")
        if current_workflow_state:
            # Ensure it's a SupervisorWorkflowState object
            if isinstance(current_workflow_state, dict):
                workflow_state_obj = SupervisorWorkflowState(**current_workflow_state)
            else:
                workflow_state_obj = current_workflow_state
        else:
            # Fallback if no workflow state exists
            workflow_state_obj = SupervisorWorkflowState(
                workflow_id=original_supervisor_state.get("task_id", ""),
                current_phase="helm_mgmt"
            )
        
        # Get the last message from the agent to extract the actual response
        messages = helm_state.get("messages", [])
        last_content = "No response from Helm Management Agent."
        
        # Find the last AIMessage (this is the agent's final response)
        for msg in reversed(messages):
            if hasattr(msg, 'type') and msg.type == "ai":
                last_content = msg.content if hasattr(msg, 'content') else str(msg)
                break
            elif isinstance(msg, dict) and msg.get("type") == "ai":
                last_content = msg.get("content", str(msg))
                break
        
        # If still no content, try to extract from any message with content
        if last_content == "No response from Helm Management Agent.":
            for msg in reversed(messages):
                if hasattr(msg, 'content'):
                    content = msg.content
                    if content and content != "No response from Helm Management Agent.":
                        last_content = str(content)
                        break
        
        # Mark helm_mgmt phase as complete (similar to validation_to_supervisor)
        workflow_state_obj.set_phase_complete("helm_mgmt")
        workflow_state_obj.last_swarm = "helm_mgmt_swarm"
        workflow_state_obj.current_phase = "helm_mgmt"  # Ensure current_phase is set
        
        # Return object directly to avoid Pydantic serialization warnings
        updated_workflow_state = workflow_state_obj
        
        # Create summary messages (similar to validation_to_supervisor pattern)
        summary_messages = [
            ToolMessage(
                content=last_content,  # Direct response, not wrapped
                tool_call_id=tool_call_id
            ),
            HumanMessage(
                content=f"Helm Management Agent completed successfully. Response: {last_content[:200]}..."
            )
        ]
        
        # Build return dictionary (similar to validation_to_supervisor)
        # IMPORTANT: Set status="completed" and active_phase to match last_swarm
        return_dict = {
            "messages": summary_messages,
            "llm_input_messages": summary_messages,
            "workflow_state": updated_workflow_state,
            "helm_mgmt_response": last_content,  # Store response for supervisor to extract
            "status": "completed",  # Mark as completed (was "pending")
            "active_phase": "helm_mgmt_swarm"  # Match last_swarm value
        }
        
        return return_dict

    # ============================================================================
    # ArgoCD Onboarding Agent Transformations
    # ============================================================================

    @staticmethod
    def supervisor_to_argocd_onboarding(supervisor_state: MainSupervisorState) -> Dict:
        """
        Transform supervisor state to ArgoCD Onboarding Agent input state.
        
        Maps:
        - user_query → user_request
        - messages → messages (initialized with user query)
        - Initializes all required ArgoCDOnboardingState fields with defaults
        """
        user_query = supervisor_state.get("user_query", "")
        messages = [HumanMessage(content=user_query)] if user_query else []
        
        return {
            # Core message history
            "messages": messages,
            
            # User request information
            "user_request": user_query,
            "user_id": "user",  # Default
            "session_id": supervisor_state.get("session_id", "default"),
            
            # Workflow type and phase
            "workflow_type": "onboarding",
            "current_phase": 1,
            
            # Cluster context
            "cluster_context": {},
            
            # ArgoCD Project State
            "project_info": {},
            "project_list": [],
            "project_created": False,
            "project_validation_result": None,
            
            # ArgoCD Repository State
            "repository_info": {},
            "repository_list": [],
            "repo_validation_result": None,
            "repo_onboarded": False,
            
            # ArgoCD Application State
            "application_info": {},
            "application_list": [],
            "application_created": False,
            "application_details": None,
            
            # Deployment/Sync State
            "sync_operation_id": None,
            "deployment_status": None,
            "health_report": None,
            "sync_status": None,
            
            # Debug Results
            "debug_results": {},
            "logs_collected": [],
            "events_collected": [],
            "metrics_collected": None,
            
            # Approvals (HITL Checkpoints)
            "approval_checkpoints": [],
            "pending_approval": False,
            "approval_status": "pending",
            "checkpoint_1_approved": False,
            "checkpoint_2_approved": False,
            "checkpoint_3_approved": False,
            "checkpoint_4_approved": False,
            
            # Audit Trail
            "audit_log": [],
            "execution_logs": [],
            
            # Error Handling
            "errors": [],
            "warnings": [],
            "last_error": None,
            "error_count": 0,
            
            # Deep Agent Compatibility
            "remaining_steps": None,
            "_seen_tool_calls": [],
            
            # Request Classification
            "request_classification": None,
            "request_type": "unknown",
            "operation_name": "",
            
            # Query Results
            "query_results": [],
            "query_formatted_response": "",
        }

    @staticmethod
    def argocd_onboarding_to_supervisor(
        argocd_state: Dict,
        original_supervisor_state: MainSupervisorState,
        tool_call_id: str
    ) -> Dict:
        """
        Transform ArgoCD Onboarding Agent output back to supervisor state updates.
        
        Maps:
        - messages → summary ToolMessage with actual response content
        - Updates workflow_state.argocd_onboarding_complete via set_phase_complete("argocd_onboarding")
        - Stores response in argocd_onboarding_response field
        """
        
        # Reconstruct workflow state object (similar to helm_mgmt_to_supervisor)
        current_workflow_state = original_supervisor_state.get("workflow_state")
        if current_workflow_state:
            # Ensure it's a SupervisorWorkflowState object
            if isinstance(current_workflow_state, dict):
                workflow_state_obj = SupervisorWorkflowState(**current_workflow_state)
            else:
                workflow_state_obj = current_workflow_state
        else:
            # Fallback if no workflow state exists
            workflow_state_obj = SupervisorWorkflowState(
                workflow_id=original_supervisor_state.get("task_id", ""),
                current_phase="argocd_onboarding"
            )
        
        # Get the last message from the agent to extract the actual response
        messages = argocd_state.get("messages", [])
        last_content = "No response from ArgoCD Onboarding Agent."
        
        # Find the last AIMessage (this is the agent's final response)
        for msg in reversed(messages):
            if hasattr(msg, 'type') and msg.type == "ai":
                last_content = msg.content if hasattr(msg, 'content') else str(msg)
                break
            elif isinstance(msg, dict) and msg.get("type") == "ai":
                last_content = msg.get("content", str(msg))
                break
        
        # If still no content, try to extract from any message with content
        if last_content == "No response from ArgoCD Onboarding Agent.":
            for msg in reversed(messages):
                if hasattr(msg, 'content'):
                    content = msg.content
                    if content and content != "No response from ArgoCD Onboarding Agent.":
                        last_content = str(content)
                        break
        
        # Build a summary from the ArgoCD state
        summary_parts = []
        
        # Project status
        if argocd_state.get("project_created"):
            project_name = argocd_state.get("project_info", {}).get("name", "unknown")
            summary_parts.append(f"✅ Project '{project_name}' created")
        
        # Repository status
        if argocd_state.get("repo_onboarded"):
            repo_url = argocd_state.get("repository_info", {}).get("url", "unknown")
            summary_parts.append(f"✅ Repository '{repo_url}' onboarded")
        
        # Application status
        if argocd_state.get("application_created"):
            app_name = argocd_state.get("application_info", {}).get("name", "unknown")
            summary_parts.append(f"✅ Application '{app_name}' created")
            
            # Sync status
            sync_status = argocd_state.get("sync_status")
            if sync_status:
                summary_parts.append(f"📊 Sync status: {sync_status}")
        
        # Errors
        errors = argocd_state.get("errors", [])
        if errors:
            summary_parts.append(f"⚠️ Errors: {len(errors)}")
        
        state_summary = " | ".join(summary_parts) if summary_parts else "Operation completed"

        # Mark argocd_onboarding phase as complete (HITL/input handling happens in the ArgoCD subgraph)
        workflow_state_obj.set_phase_complete("argocd_onboarding")
        workflow_state_obj.last_swarm = "argocd_onboarding_swarm"
        workflow_state_obj.current_phase = "argocd_onboarding"
        
        # Return object directly to avoid Pydantic serialization warnings
        updated_workflow_state = workflow_state_obj
        
        # Create summary messages (similar to helm_mgmt_to_supervisor pattern)
        summary_messages = [
            ToolMessage(
                content=last_content,  # Direct response, not wrapped
                tool_call_id=tool_call_id
            ),
            HumanMessage(
                content=f"ArgoCD Onboarding Agent completed. {state_summary}"
            )
        ]
        
        # Build return dictionary
        return_dict = {
            "messages": summary_messages,
            "llm_input_messages": summary_messages,
            "workflow_state": updated_workflow_state,
            "argocd_onboarding_response": last_content,  # Store response for supervisor to extract
            "status": "completed",
            "active_phase": "argocd_onboarding_swarm"
        }
        
        return return_dict