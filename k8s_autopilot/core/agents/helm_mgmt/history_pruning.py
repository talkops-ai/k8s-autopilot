"""
History Pruning Middleware for Helm Management Agent.

This middleware filters stale release data from conversation history after uninstall operations
to prevent the LLM from hallucinating that deleted releases still exist.
"""

from typing import Callable, Awaitable, List
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.types import Command

from k8s_autopilot.utils.logger import AgentLogger

# Logger instance
helm_mgmt_agent_logger = AgentLogger("k8sAutopilotHelmMgmtAgent")


class HistoryPruningMiddleware(AgentMiddleware):
    """
    Middleware to prune stale release data from conversation history.
    
    After successful uninstall, removes old ToolMessages that mention the deleted release
    to prevent the LLM from hallucinating that it still exists.
    """
    
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        
        # Only process uninstall operations
        if tool_name != "helm_uninstall_release":
            return await handler(request)
        
        # Execute the uninstall
        response = await handler(request)
        
        # Only prune if uninstall was successful
        if not isinstance(response, ToolMessage) or getattr(response, 'status', 'success') == 'error':
            return response
        
        # Get release name and namespace from args
        release_name = request.tool_call.get("args", {}).get("release_name", "")
        namespace = request.tool_call.get("args", {}).get("namespace", "")
        
        if not release_name:
            return response
        
        # Get messages from state
        messages = self._get_messages_from_request(request)
        if not messages:
            return response
        
        # Filter out stale messages mentioning this release
        pruned_messages = self._prune_release_messages(messages, release_name, namespace)
        
        # Log pruning activity
        num_pruned = len(messages) - len(pruned_messages)
        if num_pruned > 0:
            helm_mgmt_agent_logger.log_structured(
                level="INFO",
                message=f"Pruned {num_pruned} stale messages mentioning uninstalled release '{release_name}'",
                extra={"release_name": release_name, "namespace": namespace, "pruned_count": num_pruned}
            )
        
        # Return command to update messages
        return Command(
            update={
                "messages": pruned_messages,
                "helm_release_name": None,
                "helm_release_namespace": None,
            }
        )
    
    def _get_messages_from_request(self, request: ToolCallRequest) -> List[BaseMessage]:
        """Extract messages from request state."""
        messages = []
        if hasattr(request, 'runtime') and hasattr(request.runtime, 'state') and request.runtime.state:
            state = request.runtime.state
            messages = state.get("messages", []) if hasattr(state, 'get') else getattr(state, 'messages', [])
        elif hasattr(request, 'state'):
            state = request.state
            messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, 'messages', [])
        return messages
    
    
    def _prune_release_messages(self, messages: List[BaseMessage], release_name: str, namespace: str) -> List[BaseMessage]:
        """
        Filter messages to remove stale references to the deleted release.
        
        CRITICAL: Maintains valid message pairing for OpenAI API.
        OpenAI requires that every AIMessage with 'tool_calls' must be followed by
        corresponding ToolMessages. We must prune PAIRS (AIMessage + ToolMessage) together.
        
        Removes message pairs for:
        - helm_install_chart, helm_upgrade_release for this release
        - helm_get_release_status for this release
        - kubernetes_get_helm_releases showing this release
        """
        release_name_lower = release_name.lower()
        namespace_lower = namespace.lower() if namespace else ""
        
        # Tools that might contain release data
        release_tools = {
            "helm_install_chart",
            "helm_upgrade_release", 
            "helm_get_release_status",
            "kubernetes_get_helm_releases",
            "read_mcp_resource"  # May contain helm://releases data
        }
        
        # First pass: identify tool_call_ids that should be pruned
        tool_call_ids_to_prune = set()
        
        from langchain_core.messages import AIMessage
        
        for msg in messages:
            # Check ToolMessages for stale release data
            if isinstance(msg, ToolMessage):
                tool_name = getattr(msg, 'name', '')
                content = str(getattr(msg, 'content', '')).lower()
                
                # If this ToolMessage mentions the deleted release
                if tool_name in release_tools:
                    if release_name_lower in content:
                        if not namespace or namespace_lower in content:
                            # Mark this tool_call_id for pruning
                            tool_call_id = getattr(msg, 'tool_call_id', None)
                            if tool_call_id:
                                tool_call_ids_to_prune.add(tool_call_id)
        
        # Second pass: prune both AIMessage (with tool_calls) and corresponding ToolMessages
        pruned = []
        for msg in messages:
            should_keep = True
            
            # Check if this is an AIMessage with tool_calls that should be pruned
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
                # If ANY of its tool_calls are marked for pruning, skip the entire AIMessage
                for tool_call in msg.tool_calls:
                    tool_call_id = tool_call.get('id', '')
                    if tool_call_id in tool_call_ids_to_prune:
                        should_keep = False
                        break
            
            # Check if this is a ToolMessage marked for pruning
            elif isinstance(msg, ToolMessage):
                tool_call_id = getattr(msg, 'tool_call_id', None)
                if tool_call_id in tool_call_ids_to_prune:
                    should_keep = False
            
            if should_keep:
                pruned.append(msg)
        
        return pruned



