"""
Helm Management Deep Agent

This module implements the Helm Installation Management Agent using LangChain's
create_deep_agent with tools, resources, and prompts loaded from the FastMCP 
Helm MCP Server.

Architecture References:
- docs/deployment/helm-agent-architecture.md
- docs/deployment/fastmcp-server-architecture.md

Key Design:
- Tools/Resources/Prompts are exposed by the FastMCP server (managed via MCPAdapterClient)
- Agent loads all three via MCP client using langchain-mcp-adapters
- Middleware handles state exposure, HITL workflows, and error recovery

The agent follows a 5-phase workflow:
1. Information Gathering & Discovery
2. Planning & Validation
3. User Approval & Modifications
4. Execution & Deployment
5. Post-Installation Verification & Reporting
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional, Annotated, Callable, Awaitable, List, Set
from langgraph.types import Command, interrupt
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langchain_core.tools import BaseTool
from langchain_core.messages import BaseMessage, ToolMessage
from pydantic import BaseModel
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent, CompiledSubAgent
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.tools import StructuredTool

from k8s_autopilot.core.state.base import HelmAgentState
from k8s_autopilot.utils.logger import AgentLogger, log_sync
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from k8s_autopilot.core.agents.base_agent import BaseSubgraphAgent
from k8s_autopilot.utils.mcp_client import MCPAdapterClient
from k8s_autopilot.core.agents.helm_mgmt.helm_mgmt_prompts import (
    HELM_MGMT_SUPERVISOR_PROMPT,
    DISCOVERY_SUBAGENT_PROMPT,
    PLANNER_SUBAGENT_PROMPT,
    QUERY_SUBAGENT_PROMPT,
    HELM_BEST_PRACTICES,
)

# Agent logger
helm_mgmt_agent_logger = AgentLogger("k8sAutopilotHelmMgmtAgent")


# ============================================================================
# State Update Handlers - Separated by Concern
# ============================================================================

def _update_discovery_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for discovery tools."""
    import json
    updates = {}
    
    try:
        if isinstance(content, str):
            data = json.loads(content)
            updates["chart_search_results"] = [data]
    except Exception:
        pass
    
    return updates


def _update_execution_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for execution tools."""
    updates = {}
    status = "success" if "error" not in str(content).lower() else "failed"
    safe_args = tool_args.copy() if isinstance(tool_args, dict) else {}
    
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": tool_name,
        "status": status,
        "output": str(content)[:200] + "..." if len(str(content)) > 200 else str(content),
        "args": safe_args
    }
    
    updates["execution_logs"] = [log_entry]
    
    if status == "success" and tool_name == "helm_install_chart":
        updates["execution_status"] = "completed"
        updates["helm_release_name"] = safe_args.get("release_name")
        updates["helm_release_namespace"] = safe_args.get("namespace")
    
    return updates


def _update_validation_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for validation tools."""
    import json
    updates = {}
    
    try:
        if isinstance(content, str):
            res = json.loads(content)
            if not res.get("valid", False) and "errors" in res:
                updates["validation_errors"] = res["errors"]
                updates["validation_status"] = "failed"
            else:
                updates["validation_status"] = "passed"
    except Exception:
        pass
    
    return updates


# ============================================================================
# Message Formatters - Template-Based
# ============================================================================

def _format_discovery_message(tool_name: str, tool_args: Dict[str, Any], content: Any, status: str) -> str:
    """Format message for discovery tools - just formats nicely, no duplicate warnings."""
    import json
    try:
        if isinstance(content, str):
            data = json.loads(content)
            chart_name = data.get("name", tool_args.get("chart_name", "unknown"))
            if "/" in chart_name:
                chart_name = chart_name.split("/")[-1]
            repository = tool_args.get("repository", "")
            version = data.get("version", "unknown")
            
            # Just format the message nicely - duplicate warnings come from _check_duplicate
            return (
                f"Found Chart: {chart_name}\n"
                f"Version: {version}\n"
                f"[SYSTEM] ✅ Successfully retrieved metadata for '{chart_name}' from repository '{repository}'."
            )
    except Exception:
        pass
    return str(content)


def _format_execution_message(tool_name: str, tool_args: Dict[str, Any], content: Any, status: str) -> str:
    """Format message for execution tools."""
    release_name = tool_args.get("release_name", "")
    namespace = tool_args.get("namespace", "")
    return f"Action '{tool_name}' {status}.\nRelease: {release_name}\nNamespace: {namespace}"


def _format_validation_message(tool_name: str, tool_args: Dict[str, Any], content: Any, status: str) -> str:
    """Format message for validation tools."""
    import json
    try:
        if isinstance(content, str):
            res = json.loads(content)
            if not res.get("valid", False) and "errors" in res:
                error_count = len(res["errors"])
                top_errors = "\n".join([f"- {e.get('field', 'unknown')}: {e.get('error_message', '')}" 
                                       for e in res["errors"][:3]])
                return f"Validation FAILED with {error_count} error(s).\nTop errors:\n{top_errors}"
            return "Validation PASSED. Configuration is valid."
    except Exception:
        pass
    return str(content)


# ============================================================================
# Content Matchers - For Duplicate Detection
# ============================================================================

def _match_discovery_content(tool_name: str, current_args: Dict[str, Any], content: str) -> bool:
    """Match discovery tool content for duplicate detection."""
    chart_name = current_args.get("chart_name", "").lower()
    repository = current_args.get("repository", "").lower()
    if "/" in chart_name:
        chart_name = chart_name.split("/")[-1]
    content_lower = content.lower()
    return (chart_name and chart_name in content_lower and 
            (not repository or repository in content_lower))


def _match_execution_content(tool_name: str, current_args: Dict[str, Any], content: str) -> bool:
    """Match execution tool content for duplicate detection."""
    release_name = current_args.get("release_name", "").lower()
    namespace = current_args.get("namespace", "").lower()
    content_lower = content.lower()
    return (release_name and release_name in content_lower and 
            (not namespace or namespace in content_lower))


def _match_validation_content(tool_name: str, current_args: Dict[str, Any], content: str) -> bool:
    """Match validation tool content for duplicate detection."""
    chart_name = current_args.get("chart_name", "").lower()
    repository = current_args.get("repository", "").lower()
    content_lower = content.lower()
    return (chart_name and chart_name in content_lower and 
            (not repository or repository in content_lower))


# ============================================================================
# Tool Configuration Registry - Data-Driven Approach
# ============================================================================

class ToolConfig:
    """Configuration for tool behavior in middleware."""
    
    def __init__(
        self,
        duplicate_check_keys: tuple[str, ...],
        state_updater: Optional[Callable[[str, Dict[str, Any], Any], Dict[str, Any]]] = None,
        message_formatter: Optional[Callable[[str, Dict[str, Any], Any, str], str]] = None,
        content_matcher: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    ):
        self.duplicate_check_keys = duplicate_check_keys
        self.state_updater = state_updater
        self.message_formatter = message_formatter
        self.content_matcher = content_matcher


class ToolRegistry:
    """Registry for tool configurations - eliminates if/else chains."""
    
    def __init__(self):
        self._configs: Dict[str, ToolConfig] = {}
        self._register_defaults()
    
    def register(self, tool_name: str, config: ToolConfig):
        """Register a tool configuration."""
        self._configs[tool_name] = config
    
    def get(self, tool_name: str) -> Optional[ToolConfig]:
        """Get configuration for a tool."""
        return self._configs.get(tool_name)
    
    def has_duplicate_check(self, tool_name: str) -> bool:
        """Check if tool has duplicate detection configured."""
        return tool_name in self._configs and self._configs[tool_name].duplicate_check_keys
    
    def _register_defaults(self):
        """Register default tool configurations."""
        # Discovery tools
        for tool_name in ["helm_get_chart_info", "helm_get_chart_values_schema", "helm_list_chart_versions"]:
            self.register(tool_name, ToolConfig(
                duplicate_check_keys=("chart_name", "repository"),
                state_updater=_update_discovery_state,
                message_formatter=_format_discovery_message,
                content_matcher=_match_discovery_content,
            ))
        
        # Execution tools
        for tool_name in ["helm_install_chart", "helm_upgrade_release", 
                         "helm_rollback_release", "helm_uninstall_release"]:
            self.register(tool_name, ToolConfig(
                duplicate_check_keys=("release_name", "namespace"),
                state_updater=_update_execution_state,
                message_formatter=_format_execution_message,
                content_matcher=_match_execution_content,
            ))
        
        # Validation tools
        self.register("helm_validate_values", ToolConfig(
            duplicate_check_keys=("chart_name", "repository"),
            state_updater=_update_validation_state,
            message_formatter=_format_validation_message,
            content_matcher=_match_validation_content,
        ))


# Global registry instance
_TOOL_REGISTRY = ToolRegistry()


# ============================================================================
# Middleware for State Access, HITL, and Error Recovery
# ============================================================================

class HelmAgentStateMiddleware(AgentMiddleware):
    """
    Middleware to intercept tool outputs and update HelmAgentState.
    
    This bridges the gap between MCP tools (which return strings/JSON)
    and the Agent's structured state (chart_metadata, execution_logs, etc.)
    
    Uses a configuration-based registry pattern to eliminate if/else chains.
    """
    state_schema = HelmAgentState
    
    def __init__(self):
        self.registry = _TOOL_REGISTRY
    
    def _normalize_args(self, args: Dict[str, Any], keys: tuple[str, ...]) -> tuple:
        """Normalize tool arguments for comparison."""
        normalized = []
        for key in keys:
            value = args.get(key, "")
            normalized.append(str(value).lower().strip() if isinstance(value, str) else str(value).lower().strip())
        return tuple(normalized)
    
    def _was_tool_called_with_args(
        self, 
        messages: List[BaseMessage], 
        tool_name: str, 
        current_args: Dict[str, Any],
        config: ToolConfig
    ) -> bool:
        """
        Check message history to see if this tool was already called with the same arguments.
        
        Only matches the EXACT same tool with EXACT same arguments - no cross-tool matching.
        Uses tool_call_id matching for reliability.
        """
        arg_keys = config.duplicate_check_keys
        current_normalized = self._normalize_args(current_args, arg_keys)
        
        # Iterate through messages in reverse (most recent first)
        for message in reversed(messages):
            # Check AIMessage with tool calls - this is the primary and most reliable method
            if hasattr(message, 'tool_calls') and message.tool_calls:
                for tool_call in message.tool_calls:
                    # CRITICAL: Only check if it's the SAME tool name
                    if tool_call.get("name") == tool_name:
                        call_args = tool_call.get("args", {})
                        call_normalized = self._normalize_args(call_args, arg_keys)
                        # CRITICAL: Only match if arguments are EXACTLY the same
                        if call_normalized == current_normalized:
                            # Found matching tool call - check for successful response by tool_call_id
                            tool_call_id = tool_call.get("id", "")
                            for msg in messages:
                                # Match by tool_call_id AND tool name to ensure correctness
                                if (hasattr(msg, 'tool_call_id') and 
                                    getattr(msg, 'tool_call_id', '') == tool_call_id and
                                    hasattr(msg, 'name') and 
                                    getattr(msg, 'name', '') == tool_name):
                                    status = getattr(msg, 'status', 'success')
                                    if status == 'success':
                                        return True
        
        # No matching tool call found - this is NOT a duplicate
        return False
    
    def _check_duplicate(self, request: ToolCallRequest, messages: List[BaseMessage]) -> Optional[ToolMessage]:
        """Check for duplicate tool calls using registry configuration."""
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})
        
        config = self.registry.get(tool_name)
        if not config or not config.duplicate_check_keys:
            return None
        
        if self._was_tool_called_with_args(messages, tool_name, tool_args, config):
            arg_str = ", ".join([f"{k}={tool_args.get(k, '')}" for k in config.duplicate_check_keys])
            helm_mgmt_agent_logger.log_structured(
                level="WARNING",
                message=f"Preventing duplicate {tool_name} call with args: {arg_str}",
                extra={"tool_name": tool_name, "args": tool_args}
            )
            
            # Generate error message based on tool type
            if tool_name in {"helm_install_chart", "helm_upgrade_release", 
                            "helm_rollback_release", "helm_uninstall_release"}:
                release_name = tool_args.get("release_name", "")
                namespace = tool_args.get("namespace", "")
                error_content = (
                    f"[SYSTEM] ❌ ERROR: Duplicate tool call detected!\n"
                    f"[SYSTEM] Tool `{tool_name}` has ALREADY been called for release '{release_name}' in namespace '{namespace}'.\n"
                    f"[SYSTEM] The operation has already completed successfully. DO NOT call this tool again.\n"
                    f"[SYSTEM] \n"
                    f"[SYSTEM] ACTION REQUIRED:\n"
                    f"[SYSTEM] 1. Review the previous `{tool_name}` result in your conversation history.\n"
                    f"[SYSTEM] 2. Check the execution status - the operation is already complete.\n"
                    f"[SYSTEM] 3. If you need to verify status, use `helm_get_release_status` instead.\n"
                    f"[SYSTEM] 4. STOP calling `{tool_name}` - it will not provide new information."
                )
            else:
                error_content = (
                    f"[SYSTEM] ❌ ERROR: Duplicate tool call detected!\n"
                    f"[SYSTEM] Tool `{tool_name}` has ALREADY been called with these arguments: {arg_str}\n"
                    f"[SYSTEM] You already have the result in your conversation history. DO NOT call this tool again.\n"
                    f"[SYSTEM] \n"
                    f"[SYSTEM] ACTION REQUIRED:\n"
                    f"[SYSTEM] 1. Review the previous `{tool_name}` result in your conversation history.\n"
                    f"[SYSTEM] 2. Use the information you already have to proceed.\n"
                    f"[SYSTEM] 3. STOP calling `{tool_name}` with the same arguments - it will not provide new information."
                )
            
            return ToolMessage(
                content=error_content,
                tool_call_id=request.tool_call.get("id", ""),
                name=tool_name,
                status="error"
            )
        
        return None
    
    def _get_state_updates(
        self, 
        tool_name: str, 
        tool_args: Dict[str, Any], 
        response: ToolMessage
    ) -> Dict[str, Any]:
        """
        Get state updates for tool response using registry configuration.
        
        Returns state updates dictionary. Preserves original tool response content.
        Uses registry pattern - no if/else chains.
        """
        config = self.registry.get(tool_name)
        if not config or not config.state_updater:
            return {}
        
        content = response.content
        return config.state_updater(tool_name, tool_args, content) or {}

    def _create_safe_tool_message(self, original: ToolMessage, updates: Dict[str, Any]) -> ToolMessage:
        """Create a new ToolMessage with safe, deep-copied artifact/updates to prevent circular refs."""
        import copy
        
        # Deep copy updates to ensure no internal references/cycles
        try:
            safe_updates = copy.deepcopy(updates)
        except Exception:
             # Fallback: simple copy if deepcopy fails (e.g. locks/sockets)
             safe_updates = updates.copy()

        # Get existing artifact and ensure it's safe
        artifact = {}
        if hasattr(original, 'artifact') and isinstance(original.artifact, dict):
            try:
                artifact = copy.deepcopy(original.artifact)
            except Exception:
                # If artifact is complex/unpickleable, try shallow copy or conversion
                try:
                    artifact = original.artifact.copy()
                except Exception:
                    # Last resort: stringify or empty to prevent crash
                    artifact = {"original_data_unavailable": "Serialization failed"}
        elif hasattr(original, 'artifact') and original.artifact is not None:
            # Artifact is not a dict (e.g. list or primitive), handle safely
            try:
                import copy
                artifact = {"data": copy.deepcopy(original.artifact)}
            except Exception:
                 artifact = {"data": str(original.artifact)}
        
        # Inject state updates into the artifact
        # This allows the parent graph to process specific state changes
        # without breaking the ToolMessage flow in the sub-agent
        # artifact['_state_updates'] = safe_updates
        
        return ToolMessage(
            content=original.content,
            tool_call_id=original.tool_call_id,
            name=original.name,
            status=getattr(original, 'status', 'success'),
            # artifact=artifact
        )

    def _get_messages_from_request(self, request: ToolCallRequest) -> List[BaseMessage]:
        """Extract messages from request state - handles different state access patterns."""
        messages = []
        if hasattr(request, 'runtime') and hasattr(request.runtime, 'state') and request.runtime.state:
            state = request.runtime.state
            messages = state.get("messages", []) if hasattr(state, 'get') else getattr(state, 'messages', [])
        elif hasattr(request, 'state'):
            state = request.state
            messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, 'messages', [])
        return messages

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """
        Intercept tool calls to update state based on results.
        
        Uses registry pattern - no if/else chains. All tool behavior is configuration-driven.
        """
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})
        
        # Check for duplicate calls using registry
        messages = self._get_messages_from_request(request)
        duplicate_error = self._check_duplicate(request, messages)
        if duplicate_error:
            return duplicate_error
        
        # Execute the tool
        response = await handler(request)
        
        # Process response only if it's a ToolMessage
        if not isinstance(response, ToolMessage):
            return response
        
        # Get state updates using registry configuration
        updates = self._get_state_updates(tool_name, tool_args, response)
        
        # Apply state updates if any, otherwise return original response
        if updates:
            tool_msg = self._create_safe_tool_message(response, updates)
            return Command(update={**updates, "messages": [tool_msg]})
        
        return response


class HelmApprovalHITLMiddleware(AgentMiddleware):
    """
    Middleware to intercept high-stakes tool calls and trigger HITL approval.
    """
    
    # Tools that require human approval before execution
    APPROVAL_REQUIRED_TOOLS = {
        "helm_install_chart",
        "helm_upgrade_release", 
        "helm_rollback_release",
        "helm_uninstall_release",
    }
    
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """
        Intercept tool calls and trigger HITL for high-stakes operations.
        """
        tool_name = request.tool_call.get("name", "")
        
        # Check if this tool requires approval
        if tool_name in self.APPROVAL_REQUIRED_TOOLS:
            helm_mgmt_agent_logger.log_structured(
                level="INFO",
                message=f"HITL: Intercepting {tool_name} for approval",
                extra={"tool_name": tool_name, "args": request.tool_call.get("args", {})}
            )
            
            # Build interrupt payload for human approval
            interrupt_payload = {
                "pending_approval": {
                    "status": "approval_required",
                    "tool_name": tool_name,
                    "tool_args": request.tool_call.get("args", {}),
                    "message": self._build_approval_message(tool_name, request.tool_call.get("args", {})),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            }
            
            # Trigger interrupt and wait for human response
            human_response = interrupt(interrupt_payload)
            
            # Check if approved
            response_str = str(human_response).lower() if human_response else ""
            if "approve" in response_str or "yes" in response_str or "proceed" in response_str:
                helm_mgmt_agent_logger.log_structured(
                    level="INFO",
                    message=f"HITL: {tool_name} approved, proceeding with execution"
                )
                return await handler(request)
            else:
                helm_mgmt_agent_logger.log_structured(
                    level="INFO",
                    message=f"HITL: {tool_name} rejected by user"
                )
                return ToolMessage(
                    content=f"Operation '{tool_name}' was rejected by user. Response: {response_str}",
                    tool_call_id=request.tool_call.get("id", "")
                )
        
        return await handler(request)
    
    def _build_approval_message(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Build human-readable approval message based on tool and args."""
        chart_name = args.get('chart_name', 'unknown')
        release_name = args.get('release_name', 'unknown')
        namespace = args.get('namespace', 'default')

        if tool_name == "helm_install_chart":
            repo = args.get('repository', 'bitnami')
            return (
                f"⚠️ **INSTALLATION APPROVAL REQUIRED**\n\n"
                f"**Chart**: {chart_name}\n"
                f"**Release**: {release_name}\n"
                f"**Namespace**: {namespace}\n"
                f"**Repository**: {repo}\n\n"
                f"Do you approve this installation? (approve/reject)"
            )
        elif tool_name == "helm_upgrade_release":
            return (
                f"⚠️ **UPGRADE APPROVAL REQUIRED**\n\n"
                f"**Release**: {release_name}\n"
                f"**Chart**: {chart_name}\n"
                f"**Namespace**: {namespace}\n\n"
                f"Do you approve this upgrade? (approve/reject)"
            )
        elif tool_name == "helm_rollback_release":
            revision = args.get('revision', 'previous')
            return (
                f"⏪ **ROLLBACK APPROVAL REQUIRED**\n\n"
                f"**Release**: {release_name}\n"
                f"**Namespace**: {namespace}\n"
                f"**Target Revision**: {revision}\n\n"
                f"Do you approve this rollback? (approve/reject)"
            )
        elif tool_name == "helm_uninstall_release":
            return (
                f"🗑️ **UNINSTALL APPROVAL REQUIRED**\n\n"
                f"**Release**: {release_name}\n"
                f"**Namespace**: {namespace}\n\n"
                f"⚠️ This will remove the release and its resources.\n"
                f"Do you approve this uninstall? (approve/reject)"
            )
        else:
            return f"Approval required for {tool_name}. Proceed? (approve/reject)"


class ErrorRecoveryMiddleware(AgentMiddleware):
    """
    Middleware for handling tool errors and implementing retry logic.
    """
    
    def __init__(self, max_retries: int = 3, retry_tools: Optional[Set[str]] = None):
        self.max_retries = max_retries
        self.retry_tools = retry_tools or {
            "helm_search_charts", 
            "helm_get_chart_info", 
            "kubernetes_get_helm_releases",
            "kubernetes_get_cluster_info"
        }
    
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        
        # Only retry specific read-only/idempotent tools
        if tool_name not in self.retry_tools:
            return await handler(request)
            
        attempts = 0
        last_error = None
        
        while attempts <= self.max_retries:
            try:
                attempts += 1
                return await handler(request)
            except Exception as e:
                last_error = e
                helm_mgmt_agent_logger.log_structured(
                    level="WARNING",
                    message=f"Tool {tool_name} failed (attempt {attempts}/{self.max_retries + 1}): {e}"
                )
                if attempts > self.max_retries:
                    break
                    
        # If all retries fail, return a structured error message
        return ToolMessage(
            content=f"Error: Tool '{tool_name}' failed after {self.max_retries} retries. Reason: {str(last_error)}",
            tool_call_id=request.tool_call.get("id", ""),
            status="error"
        )


class ResourceContextMiddleware(AgentMiddleware):
    """
    Middleware to inject MCP resource context into agent conversations.
    """
    
    def __init__(self, mcp_client: Optional[MCPAdapterClient] = None):
        self.mcp_client = mcp_client
    
    async def awrap_model_call(self, request, handler):
        """
        Inject resource context before model invocation.
        """
        if self.mcp_client and hasattr(request, 'state'):
            try:
                # Need to use await since get_resources is async
                # Pass tuple to avoid unhashable type: list error
                resources = await self.mcp_client.get_resources(("kubernetes://cluster-info",))
                if resources:
                    cluster_info = resources[0] # content of resource
                    # Add cluster context to state if not present
                    if not request.state.get("cluster_context"):
                        request.state["cluster_context"] = {
                            "from_resource": True,
                            "data": str(cluster_info.content) if hasattr(cluster_info, 'content') else str(cluster_info)
                        }
            except Exception as e:
                 # Log debug but don't crash
                 pass
        
        return await handler(request)


# ============================================================================
# HITL Tool for requesting human input
# ============================================================================

@tool
def request_human_input(
    question: str,
    context: Optional[str] = None,
    phase: Optional[str] = None,
    runtime: ToolRuntime[None, HelmAgentState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = ""
) -> Command:
    """
    Request human input during workflow execution.
    
    **CRITICAL: This is the ONLY way to request human input.**
    
    Args:
        question: The question or request for the human
        context: Optional context about why feedback is needed
        phase: Optional current workflow phase
        runtime: Tool runtime for state access
        tool_call_id: Injected tool call ID
        
    Returns:
        Command: Command to update state with human response
    """
    if runtime and runtime.state:
        if not phase:
            phase = getattr(runtime.state, 'current_phase', 'discovery')
        session_id = getattr(runtime.state, 'session_id', 'unknown')
    else:
        phase = phase or "discovery"
        session_id = "unknown"
    
    interrupt_payload = {
        "pending_feedback_requests": {
            "status": "input_required",
            "session_id": session_id,
            "question": question,
            "context": context or "No additional context provided",
            "active_phase": phase,
            "tool_name": "request_human_input",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }
    
    helm_mgmt_agent_logger.log_structured(
        level="INFO",
        message="Requesting human feedback",
        extra={
            "phase": phase,
            "question_preview": question[:100] if len(question) > 100 else question,
            "session_id": session_id
        }
    )
    
    human_response = interrupt(interrupt_payload)
    human_response_str = str(human_response) if human_response else ""
    
    tool_message = ToolMessage(
        content=f"Human has responded: {human_response_str}",
        tool_call_id=tool_call_id
    )
    
    return Command(
        update={
            "messages": [tool_message],
            "user_request": human_response_str,
        },
    )


# ============================================================================
# Main Helm Management Deep Agent
# ============================================================================

class k8sAutopilotHelmMgmtAgent(BaseSubgraphAgent):
    """
    Helm Management Deep Agent for K8s Autopilot.
    
    Manages Helm chart lifecycle using a standard MCP client infrastructure.
    Configured via global Config object.
    
    References:
    - docs/deployment/helm-agent-architecture.md
    """
    
    # Tool classification for sub-agents (matching MCP server defs)
    DISCOVERY_TOOL_NAMES = {
        "helm_search_charts",
        "helm_get_chart_info",
        "helm_get_chart_values_schema",
        "helm_list_chart_versions",
        "kubernetes_get_cluster_info",
        "kubernetes_list_namespaces",
        "kubernetes_get_helm_releases",
    }
    
    PLANNER_TOOL_NAMES = {
        "helm_validate_values",
        "helm_render_manifests",
        "helm_validate_manifests",
        "helm_check_dependencies",
        "helm_get_installation_plan",
        "kubernetes_check_prerequisites",
        "kubernetes_get_cluster_info",
    }
    
    QUERY_TOOL_NAMES = {
        "kubernetes_get_helm_releases",
        "helm_get_release_status",
        "helm_get_chart_info",
        "kubernetes_get_cluster_info",
        "kubernetes_list_namespaces",
        "helm_search_charts",
        "helm_list_chart_versions",
    }
    
    EXECUTION_TOOL_NAMES = {
        "helm_install_chart",
        "helm_upgrade_release",
        "helm_rollback_release",
        "helm_uninstall_release",
        "helm_dry_run_install",
        "helm_monitor_deployment",
        "helm_get_release_status",
    }
    
    # Resources to load at initialization (Context Injection)
    # Only load global context here. Dynamic resources are fetched via tool.
    MCP_RESOURCE_URIS = {
        "kubernetes://cluster-info",
    }
    

    
    def __init__(
        self,
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        name: str = "helm_mgmt_agent",
        memory: Optional[MemorySaver] = None,
    ):
        """
        Initialize the Helm Management Agent.
        """
        self.config_instance = config or Config(custom_config or {})
        helm_mgmt_agent_logger.log_structured(
            level="INFO",
            message="Initializing k8sAutopilotHelmMgmtAgent",
            extra={"name": name}
        )
        
        self._name = name
        self.memory = memory or MemorySaver()
        
        # Initialize standard MCP client with config
        self._mcp_client = MCPAdapterClient(config=self.config_instance)
        self._mcp_tools: List[BaseTool] = []
        self._mcp_resources: Dict[str, Any] = {}
        self._mcp_prompts: Dict[str, List[BaseMessage]] = {}
        self._mcp_initialized = False
        
        self._initialize_llm()
        
        helm_mgmt_agent_logger.log_structured(
            level="INFO",
            message="k8sAutopilotHelmMgmtAgent initialized"
        )
    
    def _initialize_llm(self) -> None:
        """Initialize LLM models."""
        llm_config = self.config_instance.get_llm_config()
        llm_deepagent_config = self.config_instance.get_llm_deepagent_config()
        
        try:
            self.model = LLMProvider.create_llm(**llm_config)
            self.deep_agent_model = LLMProvider.create_llm(**llm_deepagent_config)
            
            helm_mgmt_agent_logger.log_structured(
                level="INFO",
                message=f"LLMs initialized: {llm_config['model']}, {llm_deepagent_config['model']}"
            )
        except Exception as e:
            helm_mgmt_agent_logger.log_structured(
                level="ERROR",
                message=f"Failed to initialize LLM: {e}"
            )
            raise
    
    async def initialize_mcp(self) -> None:
        """
        Initialize MCP client, tools, resources, and prompts.
        Must be called before building the graph.
        """
        if self._mcp_initialized:
            return
        
        try:
            await self._mcp_client.initialize()
            self._mcp_tools = self._mcp_client.get_tools()
            
            # Load initial resources
            try:
                # Pass tuple to avoid "unhashable type: list" error in library
                # And explicit URIs to avoid "server name None" error
                resources = await self._mcp_client.get_resources(tuple(self.MCP_RESOURCE_URIS))
                self._mcp_resources = {r.uri: r for r in resources if hasattr(r, 'uri')}
            except Exception as e:
                helm_mgmt_agent_logger.log_structured(level="WARNING", message=f"Failed to load resources: {e}")



            self._mcp_initialized = True
            
            helm_mgmt_agent_logger.log_structured(
                level="INFO",
                message="MCP integration initialized",
                extra={
                    "resource_count": len(self._mcp_resources),
                }
            )
            
        except Exception as e:
            helm_mgmt_agent_logger.log_structured(
                level="WARNING",  # Changed to WARNING as we want to degrade gracefully
                message=f"Failed to initialize MCP (Agent will run in degraded mode): {e}"
            )
            # Do NOT raise. Allow agent to start without MCP tools.
            self._mcp_initialized = True
    
    def _filter_tools_by_names(self, tool_names: Set[str]) -> List[BaseTool]:
        """Filter loaded Tools by name."""
        return [t for t in self._mcp_tools if t.name in tool_names]
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def state_model(self) -> type[BaseModel]:
        return HelmAgentState
    
    def _build_system_prompt_with_context(self) -> str:
        """
        Build system prompt.
        
        Refactoring Note:
        We intentionally avoid pre-fetching prompts from MCP during initialization 
        to ensure the agent can start even if the MCP server is offline.
        
        The agent relies on:
        1. Local 'HELM_MGMT_SUPERVISOR_PROMPT' for core behavior
        2. Local 'HELM_BEST_PRACTICES' for general context
        3. 'get_expert_guidance' tool for specific/dynamic MCP prompts (runtime)
        """
        base_prompt = HELM_MGMT_SUPERVISOR_PROMPT
        
        # Append local best practices (Static Context)
        base_prompt += f"\n\n{HELM_BEST_PRACTICES}"
        
        return base_prompt
    
    def _initialize_sub_agents(self) -> None:
        """Initialize specialized sub-agents with filtered tools."""
        
        discovery_tools = self._filter_tools_by_names(self.DISCOVERY_TOOL_NAMES)
        planner_tools = self._filter_tools_by_names(self.PLANNER_TOOL_NAMES)
        
        # Common tools for all agents
        resource_tool = self._create_resource_tool()
        
        # Create Discovery Agent with SummarizationMiddleware to prevent context overflow
        # SummarizationMiddleware condenses message history when it gets too long
        discovery_summarization_middleware = SummarizationMiddleware(
            model=self.model,
            max_tokens_before_summary=100000,  # Summarize when approaching context limit (128k max)
            messages_to_keep=10,  # Keep last 10 messages after summarization
        )
        
        self.discovery_agent = create_agent(
            model=self.model,
            system_prompt=DISCOVERY_SUBAGENT_PROMPT,
            tools=discovery_tools + [resource_tool],
            state_schema=HelmAgentState,
            middleware=[
                HelmAgentStateMiddleware(),
                ErrorRecoveryMiddleware(retry_tools={"helm_search_charts", "helm_get_chart_info"}),
                # discovery_summarization_middleware,  # Add summarization to prevent context overflow
            ],
        )
        
        self.discovery_subagent = CompiledSubAgent(
            name="discovery_agent",
            description="Specialized agent for searching and analyzing Helm charts and cluster state.",
            runnable=self.discovery_agent,
        )
        
        # Create Planner Agent with SummarizationMiddleware to prevent context overflow
        # SummarizationMiddleware condenses message history when it gets too long
        # Trigger at 100000 tokens (well before 128000 limit) to ensure safety margin
        summarization_middleware = SummarizationMiddleware(
            model=self.model,  # Use same model for summarization
            max_tokens_before_summary=100000,  # Summarize when approaching context limit (128k max)
            messages_to_keep=10,  # Keep last 10 messages after summarization
        )
        
        self.planner_agent = create_agent(
            model=self.model,
            system_prompt=PLANNER_SUBAGENT_PROMPT,
            tools=planner_tools + [resource_tool],
            state_schema=HelmAgentState,
            middleware=[
                HelmAgentStateMiddleware(),
                # summarization_middleware,  # Add summarization to prevent context overflow
            ],
        )
        self.planner_subagent = CompiledSubAgent(
            name="planner_agent",
            description=(
                "Specialized agent for validating configurations and creating installation plans. "
                "Requires chart details (name, repository, version), required configuration fields, "
                "dependencies, cluster context, and user-provided values to generate accurate plans."
            ),
            runnable=self.planner_agent,
        )
        
        # Create Query Agent for read-only operations
        query_tools = self._filter_tools_by_names(self.QUERY_TOOL_NAMES)
        
        self.query_agent = create_agent(
            model=self.model,
            system_prompt=QUERY_SUBAGENT_PROMPT,
            tools=query_tools + [resource_tool],
            state_schema=HelmAgentState,
            middleware=[
                HelmAgentStateMiddleware(),  # Track state
                ErrorRecoveryMiddleware(
                    max_retries=3,
                    retry_tools=self.QUERY_TOOL_NAMES
                ),
                # NO HITL middleware - queries don't need approval
            ],
        )
        
        self.query_subagent = CompiledSubAgent(
            name="query_agent",
            description="Answers questions about Helm releases, charts, and cluster state. Handles read-only queries with immediate responses.",
            runnable=self.query_agent,
        )
        
        self._sub_agents = [self.discovery_subagent, self.planner_subagent, self.query_subagent]
    
    def _create_resource_tool(self) -> BaseTool:
        """Create a tool that allows the agent to read MCP resources by URI."""
        
        async def read_mcp_resource(uri: str) -> str:
            """
            Read a specific resource from the MCP server.
            
            Useful for reading:
            - Chart READMEs: helm://charts/{repository}/{chart_name}/readme
            - List all helm releases: helm://releases
            - Release details: helm://releases/{release_name}
            - Metadata for specifc chart: helm://charts/{repository}/{chart_name}
            - Cluster info: kubernetes://cluster-info
            - List all namespaces: kubernetes://namespaces
            - List all available charts: helm://charts
            
            Args:
                uri: The full URI of the resource to read.
            """
            try:
                # Pass tuple to avoid "unhashable type: list" error
                resources = await self._mcp_client.get_resources((uri,))
                if not resources:
                    return f"Resource not found: {uri}"
                
                res = resources[0]
                return str(res.content) if hasattr(res, 'content') else str(res)
            except Exception as e:
                return f"Error reading resource {uri}: {str(e)}"

        return StructuredTool.from_function(
            func=None,
            coroutine=read_mcp_resource,
            name="read_mcp_resource",
            description="Read content of a specific MCP resource (e.g., chart README, cluster info)."
        )

    def _create_dynamic_prompt_tool(self) -> BaseTool:
        """Create a tool that allows the agent to fetch dynamic MCP prompts at runtime."""
        
        async def get_expert_guidance(
            topic: str,
            arguments: Dict[str, Any] = {}
        ) -> str:
            """
            Get expert guidance and best practices for specific topics.
            
            Use this to fetch dynamic guides when dealing with specific issues.
            
            Args:
                topic: The topic to get guidance on. Options:
                       - "troubleshooting": Requires 'error_type' in arguments
                       - "upgrade": Requires 'chart_name' in arguments
                       - "rollback": Requires 'release_name' in arguments
                arguments: Dictionary of arguments required for the specific topic.
                           Example: {"error_type": "pod-crashloop"}
                           
            Returns:
                The guidance text.
            """
            prompt_map = {
                "troubleshooting": "helm-troubleshooting-guide",
                "upgrade": "helm-upgrade-guide",
                "rollback": "helm-rollback-procedures"
            }
            
            prompt_name = prompt_map.get(topic)
            if not prompt_name:
                return f"Unknown topic '{topic}'. Available: {list(prompt_map.keys())}"
                
            try:
                msgs = await self._mcp_client.get_prompt(prompt_name, arguments)
                if not msgs:
                    return f"No guidance found for {topic}."
                
                # Combine messages into a single string response
                return "\n".join(
                    str(msg.content) if hasattr(msg, 'content') else str(msg)
                    for msg in msgs
                )
            except Exception as e:
                return f"Error fetching guidance: {str(e)}"

        return StructuredTool.from_function(
            func=None,
            coroutine=get_expert_guidance,
            name="get_expert_guidance",
            description="Fetch expert guidance and troubleshooting steps for specific situations."
        )

    def build_graph(self) -> StateGraph:
        """
        Build the deep agent graph.
        
        The supervisor LLM intelligently delegates to sub-agents:
        - query_agent: For read-only queries (list, status, describe, search)
        - discovery_agent: For gathering chart information
        - planner_agent: For validation and planning
        
        No separate classifier node needed - the supervisor prompt guides routing decisions.
        """
        # Connection might be closed by factory for safe transport across loops,
        # but tools should be loaded.
        
        self._initialize_sub_agents()
        execution_tools = self._filter_tools_by_names(self.EXECUTION_TOOL_NAMES)
        
        # Add dynamic prompt and resource capabilities
        dynamic_prompt_tool = self._create_dynamic_prompt_tool()
        resource_tool = self._create_resource_tool()
        
        supervisor_tools = execution_tools + [request_human_input, dynamic_prompt_tool, resource_tool]
        
        enriched_prompt = self._build_system_prompt_with_context()
        
        # Create deep agent - supervisor will intelligently route to query_agent, discovery_agent, or planner_agent
        self.helm_mgmt_agent = create_deep_agent(
            model=self.deep_agent_model,
            system_prompt=enriched_prompt,
            tools=supervisor_tools,
            subagents=self._sub_agents,  # Includes query_agent, discovery_agent, planner_agent
            checkpointer=self.memory,
            context_schema=HelmAgentState,
            middleware=[
                HelmAgentStateMiddleware(),
                HelmApprovalHITLMiddleware(),
                ErrorRecoveryMiddleware(), # Default retries for execution phase
                ResourceContextMiddleware(mcp_client=self._mcp_client),
            ],
        )
        
        return self.helm_mgmt_agent
    
    async def close(self) -> None:
        if self._mcp_client:
            await self._mcp_client.close()
            self._mcp_initialized = False


# ============================================================================
# Factory Functions
# ============================================================================

@log_sync
async def create_helm_mgmt_deep_agent(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "helm_mgmt_deep_agent",
    memory: Optional[MemorySaver] = None,
) -> k8sAutopilotHelmMgmtAgent:
    """
    Factory to create and initialize Helm Mgmt Agent.
    """
    agent = k8sAutopilotHelmMgmtAgent(
        config=config,
        custom_config=custom_config,
        name=name,
        memory=memory,
    )
    await agent.initialize_mcp()
    # Close the MCP client here because this factory is often run in a temporary event loop (asyncio.run).
    # The agent's tools are now Proxies that will lazy-initialize the MCP client
    # when run in the final server event loop.
    await agent.close()
    return agent

async def create_helm_mgmt_deep_agent_factory(config: Config) -> k8sAutopilotHelmMgmtAgent:
    return await create_helm_mgmt_deep_agent(config=config)
