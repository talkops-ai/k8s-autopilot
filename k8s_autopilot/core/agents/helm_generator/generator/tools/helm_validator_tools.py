from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command, interrupt
from langchain_core.messages import ToolMessage
from k8s_autopilot.utils.logger import AgentLogger
from typing_extensions import Annotated
from k8s_autopilot.core.state.base import ValidationSwarmState, ValidationResult
from datetime import datetime
import subprocess
from typing import Dict, Any, Optional
import yaml

helm_validator_tool_logger = AgentLogger("HelmValidatorTool")


@tool
def helm_lint_validator(
    chart_path: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Validate Helm chart syntax with helm lint.
    
    This tool runs `helm lint` to validate the Helm chart structure, syntax, and best practices.
    It updates the ValidationSwarmState with validation results and blocking issues if any.
    
    Args:
        chart_path: Path to the Helm chart directory to validate
        runtime: Tool runtime from the validation swarm
        tool_call_id: Injected tool call ID for ToolMessage creation
    
    Returns:
        Command: Command to update ValidationSwarmState with validation results
    """
    try:
        # Get current retry count from state
        current_retry_counts = runtime.state.get("validation_retry_counts", {}) or {}
        current_retry_count = current_retry_counts.get("helm_lint", 0)
        
        helm_validator_tool_logger.log_structured(
            level="INFO",
            message="Starting helm lint validation",
            extra={
                "chart_path": chart_path,
                "tool_call_id": tool_call_id,
                "current_retry_count": current_retry_count
            }
        )
        
        # Run helm lint
        result = subprocess.run(
            ["helm", "lint", chart_path],
            capture_output=True,
            text=True,
            check=False,  # Don't raise on non-zero exit
            timeout=60
        )
        
        # Parse output for error/warning count
        errors_count = result.stderr.count("ERROR") + result.stdout.count("ERROR")
        warnings_count = result.stderr.count("WARNING") + result.stdout.count("WARNING")
        
        # Determine validation status and severity
        passed = result.returncode == 0
        if errors_count > 0:
            severity = "error"
        elif warnings_count > 0:
            severity = "warning"
        else:
            severity = "info"
        
        # Create validation result details
        details: Dict[str, Any] = {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "errors_count": errors_count,
            "warnings_count": warnings_count,
            "chart_path": chart_path,
            "retry_count": current_retry_count
        }
        
        # Create message with retry information
        if passed:
            message = f"Helm lint validation passed. {warnings_count} warning(s) found."
        else:
            message = f"Helm lint validation failed. {errors_count} error(s), {warnings_count} warning(s) found. (Retry attempt: {current_retry_count}/2)"
            if current_retry_count >= 2:
                message += " ⚠️ MAX RETRIES REACHED - Call ask_human for assistance."
        
        # Create ValidationResult
        validation_result = ValidationResult(
            validator="helm_lint",
            passed=passed,
            severity=severity,
            message=message,
            details=details,
            timestamp=datetime.utcnow()
        )
        
        # Create ToolMessage
        tool_message = ToolMessage(
            content=message,
            tool_call_id=tool_call_id
        )
        
        # Prepare state updates
        update_dict: Dict[str, Any] = {
            "messages": [tool_message],
            "validation_results": [validation_result]
        }
        
        # Add blocking issues and update retry count if validation failed
        if not passed:
            # Increment retry count
            new_retry_count = current_retry_count + 1
            updated_retry_counts = {**current_retry_counts, "helm_lint": new_retry_count}
            update_dict["validation_retry_counts"] = updated_retry_counts
            
            blocking_message = f"Helm lint validation failed (attempt {new_retry_count}/2): {result.stderr[:500] if result.stderr else result.stdout[:500]}"
            if new_retry_count >= 2:
                blocking_message += " ⚠️ MAX RETRIES REACHED - Use ask_human tool for assistance."
            update_dict["blocking_issues"] = [blocking_message]
            
            helm_validator_tool_logger.log_structured(
                level="WARNING",
                message="Helm lint validation failed",
                extra={
                    "chart_path": chart_path,
                    "errors_count": errors_count,
                    "warnings_count": warnings_count,
                    "retry_count": new_retry_count,
                    "max_retries_reached": new_retry_count >= 2,
                    "stderr": result.stderr[:200]
                }
            )
        else:
            # Reset retry count on success
            updated_retry_counts = {**current_retry_counts, "helm_lint": 0}
            update_dict["validation_retry_counts"] = updated_retry_counts
            
            helm_validator_tool_logger.log_structured(
                level="INFO",
                message="Helm lint validation passed",
                extra={
                    "chart_path": chart_path,
                    "warnings_count": warnings_count
                }
            )
        
        return Command(update=update_dict)
        
    except subprocess.TimeoutExpired:
        error_message = "Helm lint validation timed out after 60 seconds"
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={"chart_path": chart_path, "tool_call_id": tool_call_id}
        )
        
        validation_result = ValidationResult(
            validator="helm_lint",
            passed=False,
            severity="error",
            message=error_message,
            details={"error": "timeout", "chart_path": chart_path},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )
        
    except FileNotFoundError:
        error_message = "Helm command not found. Please ensure Helm is installed and in PATH."
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={"chart_path": chart_path, "tool_call_id": tool_call_id}
        )
        
        validation_result = ValidationResult(
            validator="helm_lint",
            passed=False,
            severity="critical",
            message=error_message,
            details={"error": "helm_not_found", "chart_path": chart_path},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )
        
    except Exception as e:
        error_message = f"Unexpected error during helm lint validation: {str(e)}"
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={
                "chart_path": chart_path,
                "tool_call_id": tool_call_id,
                "error": str(e)
            }
        )
        
        validation_result = ValidationResult(
            validator="helm_lint",
            passed=False,
            severity="error",
            message=error_message,
            details={"error": str(e), "chart_path": chart_path},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )


@tool
def helm_template_validator(
    chart_path: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    values_file: Optional[str] = None,
) -> Command:
    """
    Render Helm templates to validate syntax and YAML structure.
    
    This tool runs `helm template` to render the Helm chart templates and validates
    that the output is valid YAML. It updates the ValidationSwarmState with validation results.
    
    Args:
        chart_path: Path to the Helm chart directory to validate
        runtime: Tool runtime from the validation swarm
        tool_call_id: Injected tool call ID for ToolMessage creation
        values_file: Optional path to values file to use for templating
    
    Returns:
        Command: Command to update ValidationSwarmState with validation results
    """
    try:
        # Get current retry count from state
        current_retry_counts = runtime.state.get("validation_retry_counts", {}) or {}
        current_retry_count = current_retry_counts.get("helm_template", 0)
        
        helm_validator_tool_logger.log_structured(
            level="INFO",
            message="Starting helm template validation",
            extra={
                "chart_path": chart_path,
                "values_file": values_file,
                "tool_call_id": tool_call_id,
                "current_retry_count": current_retry_count
            }
        )
        
        # Build helm template command
        cmd = ["helm", "template", "release", chart_path]
        if values_file:
            cmd.extend(["-f", values_file])
        
        # Run helm template
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60
        )
        
        # Try to parse as YAML to validate
        yaml_valid = False
        yaml_error = None
        if result.returncode == 0:
            try:
                # Parse all YAML documents
                list(yaml.safe_load_all(result.stdout))
                yaml_valid = True
            except yaml.YAMLError as e:
                yaml_valid = False
                yaml_error = str(e)
        else:
            yaml_valid = False
        
        # Determine validation status
        passed = result.returncode == 0 and yaml_valid
        if not passed:
            if result.returncode != 0:
                severity = "error"
            elif yaml_error:
                severity = "error"
            else:
                severity = "warning"
        else:
            severity = "info"
        
        # Create validation result details
        details: Dict[str, Any] = {
            "exit_code": result.returncode,
            "yaml_valid": yaml_valid,
            "rendered_output": result.stdout if result.returncode == 0 else None,
            "error": result.stderr,
            "yaml_error": yaml_error,
            "chart_path": chart_path,
            "values_file": values_file,
            "retry_count": current_retry_count
        }
        
        # Create message with retry information
        if passed:
            message = "Helm template validation passed. Templates rendered successfully and YAML is valid."
        elif yaml_error:
            message = f"Helm template validation failed: Invalid YAML - {yaml_error[:200]} (Retry attempt: {current_retry_count}/2)"
            if current_retry_count >= 2:
                message += " ⚠️ MAX RETRIES REACHED - Call ask_human for assistance."
        else:
            message = f"Helm template validation failed: {result.stderr[:200] if result.stderr else 'Unknown error'} (Retry attempt: {current_retry_count}/2)"
            if current_retry_count >= 2:
                message += " ⚠️ MAX RETRIES REACHED - Call ask_human for assistance."
        
        # Create ValidationResult
        validation_result = ValidationResult(
            validator="helm_template",
            passed=passed,
            severity=severity,
            message=message,
            details=details,
            timestamp=datetime.utcnow()
        )
        
        # Create ToolMessage
        tool_message = ToolMessage(
            content=message,
            tool_call_id=tool_call_id
        )
        
        # Prepare state updates
        update_dict: Dict[str, Any] = {
            "messages": [tool_message],
            "validation_results": [validation_result]
        }
        
        # Add blocking issues and update retry count if validation failed
        if not passed:
            # Increment retry count
            new_retry_count = current_retry_count + 1
            updated_retry_counts = {**current_retry_counts, "helm_template": new_retry_count}
            update_dict["validation_retry_counts"] = updated_retry_counts
            
            if yaml_error:
                blocking_message = f"Helm template YAML validation failed (attempt {new_retry_count}/2): {yaml_error[:500]}"
            else:
                blocking_message = f"Helm template validation failed (attempt {new_retry_count}/2): {result.stderr[:500] if result.stderr else result.stdout[:500]}"
            
            if new_retry_count >= 2:
                blocking_message += " ⚠️ MAX RETRIES REACHED - Use ask_human tool for assistance."
            update_dict["blocking_issues"] = [blocking_message]
            
            helm_validator_tool_logger.log_structured(
                level="WARNING",
                message="Helm template validation failed",
                extra={
                    "chart_path": chart_path,
                    "values_file": values_file,
                    "yaml_valid": yaml_valid,
                    "exit_code": result.returncode,
                    "yaml_error": yaml_error,
                    "retry_count": new_retry_count,
                    "max_retries_reached": new_retry_count >= 2
                }
            )
        else:
            # Reset retry count on success
            updated_retry_counts = {**current_retry_counts, "helm_template": 0}
            update_dict["validation_retry_counts"] = updated_retry_counts
            
            helm_validator_tool_logger.log_structured(
                level="INFO",
                message="Helm template validation passed",
                extra={
                    "chart_path": chart_path,
                    "values_file": values_file
                }
            )
        
        return Command(update=update_dict)
        
    except subprocess.TimeoutExpired:
        error_message = "Helm template validation timed out after 60 seconds"
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={"chart_path": chart_path, "values_file": values_file, "tool_call_id": tool_call_id}
        )
        
        validation_result = ValidationResult(
            validator="helm_template",
            passed=False,
            severity="error",
            message=error_message,
            details={"error": "timeout", "chart_path": chart_path, "values_file": values_file},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )
        
    except FileNotFoundError:
        error_message = "Helm command not found. Please ensure Helm is installed and in PATH."
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={"chart_path": chart_path, "tool_call_id": tool_call_id}
        )
        
        validation_result = ValidationResult(
            validator="helm_template",
            passed=False,
            severity="critical",
            message=error_message,
            details={"error": "helm_not_found", "chart_path": chart_path},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )
        
    except Exception as e:
        error_message = f"Unexpected error during helm template validation: {str(e)}"
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={
                "chart_path": chart_path,
                "values_file": values_file,
                "tool_call_id": tool_call_id,
                "error": str(e)
            }
        )
        
        validation_result = ValidationResult(
            validator="helm_template",
            passed=False,
            severity="error",
            message=error_message,
            details={"error": str(e), "chart_path": chart_path, "values_file": values_file},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )


@tool
def helm_dry_run_validator(
    chart_path: str,
    release_name: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    namespace: str = "default",
) -> Command:
    """
    Run helm install --dry-run against live K8s cluster to validate chart deployment.
    
    This tool runs `helm install --dry-run --debug` to simulate installing the chart
    against a live Kubernetes cluster. Requires kubectl configured and active cluster connection.
    It updates the ValidationSwarmState with validation results.
    
    Args:
        chart_path: Path to the Helm chart directory to validate
        release_name: Name for the Helm release
        runtime: Tool runtime from the validation swarm
        tool_call_id: Injected tool call ID for ToolMessage creation
        namespace: Kubernetes namespace for the dry-run (default: "default")
    
    Returns:
        Command: Command to update ValidationSwarmState with validation results
    """
    try:
        # Get current retry count from state
        current_retry_counts = runtime.state.get("validation_retry_counts", {}) or {}
        current_retry_count = current_retry_counts.get("helm_dry_run", 0)
        
        helm_validator_tool_logger.log_structured(
            level="INFO",
            message="Starting helm dry-run validation",
            extra={
                "chart_path": chart_path,
                "release_name": release_name,
                "namespace": namespace,
                "tool_call_id": tool_call_id,
                "current_retry_count": current_retry_count
            }
        )
        
        # Run helm install --dry-run
        result = subprocess.run(
            [
                "helm", "install", release_name, chart_path,
                "--namespace", namespace,
                "--dry-run", "--debug"
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120
        )
        
        # Determine validation status
        passed = result.returncode == 0
        if not passed:
            # Check for common error patterns
            error_output = result.stderr or result.stdout
            if "Error" in error_output or "error" in error_output.lower():
                severity = "error"
            else:
                severity = "warning"
        else:
            severity = "info"
        
        # Create validation result details
        details: Dict[str, Any] = {
            "exit_code": result.returncode,
            "output": result.stdout,
            "error": result.stderr,
            "manifest": result.stdout if result.returncode == 0 else None,
            "chart_path": chart_path,
            "release_name": release_name,
            "namespace": namespace,
            "retry_count": current_retry_count
        }
        
        # Create message with retry information
        if passed:
            message = f"Helm dry-run validation passed. Chart can be installed successfully in namespace '{namespace}'."
        else:
            error_preview = result.stderr[:200] if result.stderr else result.stdout[:200]
            message = f"Helm dry-run validation failed: {error_preview} (Retry attempt: {current_retry_count}/2)"
            if current_retry_count >= 2:
                message += " ⚠️ MAX RETRIES REACHED - Call ask_human for assistance."
        
        # Create ValidationResult
        validation_result = ValidationResult(
            validator="helm_dry_run",
            passed=passed,
            severity=severity,
            message=message,
            details=details,
            timestamp=datetime.utcnow()
        )
        
        # Create ToolMessage
        tool_message = ToolMessage(
            content=message,
            tool_call_id=tool_call_id
        )
        
        # Prepare state updates
        update_dict: Dict[str, Any] = {
            "messages": [tool_message],
            "validation_results": [validation_result]
        }
        
        # Add blocking issues and update retry count if validation failed
        if not passed:
            # Increment retry count
            new_retry_count = current_retry_count + 1
            updated_retry_counts = {**current_retry_counts, "helm_dry_run": new_retry_count}
            update_dict["validation_retry_counts"] = updated_retry_counts
            
            error_output = result.stderr[:500] if result.stderr else result.stdout[:500]
            blocking_message = f"Helm dry-run validation failed (attempt {new_retry_count}/2): {error_output}"
            if new_retry_count >= 2:
                blocking_message += " ⚠️ MAX RETRIES REACHED - Use ask_human tool for assistance."
            update_dict["blocking_issues"] = [blocking_message]
            
            helm_validator_tool_logger.log_structured(
                level="WARNING",
                message="Helm dry-run validation failed",
                extra={
                    "chart_path": chart_path,
                    "release_name": release_name,
                    "namespace": namespace,
                    "exit_code": result.returncode,
                    "error_preview": error_output[:200],
                    "retry_count": new_retry_count,
                    "max_retries_reached": new_retry_count >= 2
                }
            )
        else:
            # Reset retry count on success
            updated_retry_counts = {**current_retry_counts, "helm_dry_run": 0}
            update_dict["validation_retry_counts"] = updated_retry_counts
            
            helm_validator_tool_logger.log_structured(
                level="INFO",
                message="Helm dry-run validation passed",
                extra={
                    "chart_path": chart_path,
                    "release_name": release_name,
                    "namespace": namespace
                }
            )
        
        return Command(update=update_dict)
        
    except subprocess.TimeoutExpired:
        error_message = "Helm dry-run validation timed out after 120 seconds"
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={
                "chart_path": chart_path,
                "release_name": release_name,
                "namespace": namespace,
                "tool_call_id": tool_call_id
            }
        )
        
        validation_result = ValidationResult(
            validator="helm_dry_run",
            passed=False,
            severity="error",
            message=error_message,
            details={
                "error": "timeout",
                "chart_path": chart_path,
                "release_name": release_name,
                "namespace": namespace
            },
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )
        
    except FileNotFoundError:
        error_message = "Helm command not found. Please ensure Helm is installed and in PATH."
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={"chart_path": chart_path, "tool_call_id": tool_call_id}
        )
        
        validation_result = ValidationResult(
            validator="helm_dry_run",
            passed=False,
            severity="critical",
            message=error_message,
            details={"error": "helm_not_found", "chart_path": chart_path},
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )
        
    except Exception as e:
        error_message = f"Unexpected error during helm dry-run validation: {str(e)}"
        helm_validator_tool_logger.log_structured(
            level="ERROR",
            message=error_message,
            extra={
                "chart_path": chart_path,
                "release_name": release_name,
                "namespace": namespace,
                "tool_call_id": tool_call_id,
                "error": str(e)
            }
        )
        
        validation_result = ValidationResult(
            validator="helm_dry_run",
            passed=False,
            severity="error",
            message=error_message,
            details={
                "error": str(e),
                "chart_path": chart_path,
                "release_name": release_name,
                "namespace": namespace
            },
            timestamp=datetime.utcnow()
        )
        
        tool_message = ToolMessage(
            content=error_message,
            tool_call_id=tool_call_id
        )
        
        return Command(
            update={
                "messages": [tool_message],
                "validation_results": [validation_result],
                "blocking_issues": [error_message]
            }
        )


@tool
def ask_human(
    question: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Ask the human user a question to resolve a blocking issue or get missing information.
    
    Use this tool when:
    1. You cannot fix a validation error yourself (e.g., missing specific configuration).
    2. You need to make a decision that requires human judgment.
    3. You need to confirm a critical action.
    
    The agent will PAUSE until the user responds.
    
    Args:
        question: The question to ask the user. Be specific about what information you need.
        runtime: Tool runtime.
        tool_call_id: Tool call ID.
        
    Returns:
        Command: Updates the conversation with the user's response.
    """
    helm_validator_tool_logger.log_structured(
        level="INFO",
        message="Asking human user a question",
        extra={
            "question": question,
            "tool_call_id": tool_call_id
        }
    )
    
    # Prepare interrupt payload with additional context
    interrupt_payload = {
        "pending_feedback_requests": {
            "status": "input_required",
            "question": question,
            "context": "Validation agent needs human assistance",
            "active_phase": "validation",
            "tool_call_id": tool_call_id
        }
    }
    
    # Trigger interrupt - this MUST propagate up to the graph executor
    # The interrupt() function raises a GraphInterrupt exception
    # which is caught by the graph executor to pause execution
    # DO NOT wrap this in a try-except that catches the interrupt!
    user_feedback = interrupt(interrupt_payload)
    
    # This code only executes AFTER the user resumes the workflow
    helm_validator_tool_logger.log_structured(
        level="INFO",
        message="Received human feedback after interrupt",
        extra={
            "user_feedback": str(user_feedback)[:200],
            "tool_call_id": tool_call_id
        }
    )
    
    # Create ToolMessage with the user's response
    # We prefix with "User response: " to clearly distinguish it
    tool_message = ToolMessage(
        content=f"User response: {user_feedback}",
        tool_call_id=tool_call_id
    )
    
    return Command(
        update={
            "messages": [tool_message]
        }
    )