import json
import os
from contextlib import asynccontextmanager, AsyncExitStack
from typing import Any, Dict, List, Optional, Union

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.messages import BaseMessage
from k8s_autopilot.utils.exceptions import ConfigError, K8sAutoPilotAgentError
from k8s_autopilot.config.config import Config

# ============================================================================
# HTTPX Timeout Patch
# The underlying langchain-mcp-adapters library does not expose timeout config.
# We patch httpx.AsyncClient to ensure longer timeouts for MCP connections.
# ============================================================================
import httpx

_orig_async_client_init = httpx.AsyncClient.__init__

def _patched_async_client_init(self, *args, **kwargs):
    # Set a generous default timeout (60s total, 15s connect) if not specified
    if "timeout" not in kwargs:
        kwargs["timeout"] = httpx.Timeout(600.0, connect=300.0)
    return _orig_async_client_init(self, *args, **kwargs)

httpx.AsyncClient.__init__ = _patched_async_client_init
# ============================================================================


class MCPAdapterClient:
    """
    Production-grade MCP client wrapper using langchain-mcp-adapter.
    
    Features:
    - Multi-server support (Helm, ArgoCD, etc.)
    - Tool, Resource, and Prompt support
    - Configuration driven via k8s_autopilot.config.Config
    - Robust error handling and logging
    """
    
    def __init__(self, config: Optional[Config] = None):
        """
        Initialize the MCP adapter client.
        
        Args:
            config: Configuration object containing MCP server details.
                    If None, it tries to load default configuration.
        """
        self.config = config or Config()
        self.client: Optional[MultiServerMCPClient] = None
        self._exit_stack = AsyncExitStack()
        self.tools: List[BaseTool] = []
        self._tool_map: Dict[str, BaseTool] = {}
        self._sessions: Dict[str, Any] = {} # Persistent sessions keyed by server name
        
        # Build MCP server configuration from Config object
        self.mcp_config = self._build_mcp_config()
    
    def _build_mcp_config(self) -> Dict[str, Any]:
        """
        Build MCP server configuration for MultiServerMCPClient.
        Iterates through supported servers defined in Config.
        """
        servers = {}
        
        # 1. Helm MCP Server
        helm_config = getattr(self.config, 'helm_mcp_config', {})
        if helm_config and not helm_config.get('disabled', False):
            host = helm_config.get('host', 'localhost')
            port = helm_config.get('port', 10100)
            transport = helm_config.get('transport', 'sse')
            
            if transport == 'sse':
                servers['helm_mcp_server'] = {
                    "url": f"http://{host}:{port}/sse",
                    "transport": "sse"
                }
            elif transport == 'stdio':
                 # Assuming a standard location or command for stdio if ever needed
                 # This is a placeholder as stdio usually requires specific command paths
                pass

        # 2. ArgoCD MCP Server (Example for future/other usage)
        # argocd_config = ...
        
        return servers
    


    def _create_tool_wrapper(self, original_tool: BaseTool, server_name: str) -> BaseTool:
        """
        Create a wrapper/proxy for an MCP tool.
        This allows the tool to be defined in one event loop but executed in another,
        by delegating execution to the client which manages the active session.
        """
        tool_name = original_tool.name
        
        async def _tool_proxy_func(**kwargs) -> Any:
            return await self.execute_tool(server_name, tool_name, kwargs)
            
        # Create a new tool that mimics the original but calls our proxy
        return StructuredTool.from_function(
            func=None,
            coroutine=_tool_proxy_func,
            name=tool_name,
            description=original_tool.description,
            args_schema=original_tool.args_schema,
        )

    async def execute_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Execute a tool on the specified server, ensuring a valid session exists.
        """
        # Lazy initialization if needed
        if not self._sessions:
            await self.initialize()
            
        session = self._sessions.get(server_name)
        if not session:
             # Fallback: if server_name is generic/unknown, try the first available session?
             # But for safety, we should be strict if we mapped it.
             # However, existing logic might rely on implicit mapping.
             # Let's try to find the tool if server_name is potentially stale?
             # Actually, _create_tool_wrapper binds the server_name.
             # If config changed, it might fail. Assume consistent config.
             
             if not self._sessions:
                 raise K8sAutoPilotAgentError("MCP client failed to initialize or no sessions available.")
             
             # If specific server not found, maybe just pick one (unsafe?)
             # Let's try to match by name?
             session = self._sessions.get(server_name)
             if not session:
                 raise K8sAutoPilotAgentError(f"MCP server '{server_name}' not connected.")

        # Execute tool using low-level session
        result = await session.call_tool(tool_name, arguments=arguments)
        
        # Result is CallToolResult. content is list of (TextContent | ImageContent | EmbeddedResource)
        # We need to return a string or artifacts typically
        output = []
        for content in result.content:
            if hasattr(content, 'text'):
                output.append(content.text)
            else:
                 output.append(str(content))
        
        return "\n".join(output)

    async def initialize(self) -> None:
        """Initialize the MCP client connection and load tools."""
        if self._sessions:
            return

        if not self.mcp_config:
            # If no servers are configured, we warn but don't crash
            print("Warning: No MCP servers configured.")
            return

        # Initialize the MultiServerMCPClient (factory)
        self.client = MultiServerMCPClient(self.mcp_config)
        
        # Connect to each configured server and maintain persistent sessions
        from langchain_mcp_adapters.tools import load_mcp_tools
        from langchain_core.tools import StructuredTool

        try:
            new_tools = []
            for server_name in self.mcp_config:
                # Enter persistent session context
                session = await self._exit_stack.enter_async_context(
                    self.client.session(server_name)
                )
                self._sessions[server_name] = session
                
                # Load raw tools from this session
                server_tools = await load_mcp_tools(session)
                
                # Wrap tools
                for tool in server_tools:
                     wrapper = self._create_tool_wrapper(tool, server_name)
                     new_tools.append(wrapper)
            
            # If we re-initialized, we might want to update self.tools?
            # But if we are re-initializing in a new loop, self.tools might already exist (orphans).
            # If we replace them, the Agent (which holds old tools) won't see new ones.
            # CRITICAL: The wrapper logic relies on 'self'. 
            # The 'wrapper' created in Loop A calls 'self.execute_tool'.
            # 'self.execute_tool' uses 'self._sessions'.
            # 'self._sessions' is updated here in Loop B.
            # So the OLD wrappers (held by Agent) will correctly see the NEW sessions via 'self'.
            # So we only need to populate self.tools on first run.
            if not self.tools:
                self.tools = new_tools
                self._tool_map = {tool.name: tool for tool in self.tools}
            
        except Exception as e:
            await self.close()
            raise K8sAutoPilotAgentError(f"Failed to initialize MCP client tools: {e}")
    
    async def close(self) -> None:
        """Close the MCP client connection."""
        await self._exit_stack.aclose()
        self._sessions.clear()
        self.client = None
    
    @asynccontextmanager
    async def session(self):
        """Context manager for MCP client session."""
        await self.initialize()
        try:
            yield self
        finally:
            await self.close()
    
    def get_tools(self) -> List[BaseTool]:
        """Get the loaded LangChain tools."""
        return self.tools
        
    def get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """Get a specific tool by name."""
        return self._tool_map.get(tool_name)

    async def get_resources(self, uris: Optional[List[str]] = None, server_name: str = 'helm_mcp_server') -> List[Any]:
        """
        Fetch resources from valid MCP servers.
        
        Args:
            uris: List of resource URIs to fetch. If None, fetch all available.
            server_name: Which server to query (default: 'helm_mcp_server')
        """
        # Lazy init
        if not self._sessions:
            await self.initialize()

        session = self._sessions.get(server_name)
        if not session:
            # Try finding any session if specific one not found (fallback)
            if self._sessions:
                session = next(iter(self._sessions.values()))
            else:
                raise K8sAutoPilotAgentError("MCP client not initialized or server not found")
        
        if uris:
            # If URIs are provided, fetch their actual content
            # This allows the middleware to get the data (text/json) of the resource
            results = []
            for uri in uris:
                try:
                    # read_resource returns ReadResourceResult via some adapter
                    # We accept that read_resource might fail if URI is invalid
                    resource_content = await session.read_resource(uri)
                    results.append(resource_content)
                except Exception as e:
                    # Log error but continue? Or raise?
                    # For agent robustness, we might want to skip invalid ones or log
                    print(f"Error reading resource {uri}: {e}")
                    
            return results

        # If no URIs provided, return the discovery list (metadata only)
        # Use low-level MCP session to list resources
        # Result type: ListResourcesResult
        result = await session.list_resources()
        return result.resources

    async def get_prompt(self, prompt_name: str, arguments: Optional[Dict[str, Any]] = None, server_name: str = 'helm_mcp_server') -> List[BaseMessage]:
        """
        Fetch a prompt from a specific MCP server.
        
        Args:
            prompt_name: Name of the prompt
            arguments: Arguments for the prompt template
            server_name: Which server to query (default: 'helm_mcp_server')
        """
        # Lazy init
        if not self._sessions:
            await self.initialize()

        session = self._sessions.get(server_name)
        if not session:
             raise K8sAutoPilotAgentError(f"MCP server '{server_name}' not found or not initialized")
        
        # Use low-level MCP session to get prompt
        # Result type: GetPromptResult
        result = await session.get_prompt(prompt_name, arguments=arguments or {})
        
        # Convert MCP GetPromptResult to LangChain Messages
        # result.messages is List[PromptMessage]
        # PromptMessage has 'role' and 'content' (TextContent | ImageContent | EmbeddedResource)
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage
        
        lc_messages = []
        for msg in result.messages:
            content_str = ""
            if hasattr(msg.content, 'text'):
                 content_str = msg.content.text
            elif isinstance(msg.content, list):
                 # Handle list of content objects?
                 # Assuming simple text for now based on typical usage
                 pass
            else:
                 content_str = str(msg.content)
                 
            if msg.role == 'user':
                lc_messages.append(HumanMessage(content=content_str))
            elif msg.role == 'assistant':
                lc_messages.append(AIMessage(content=content_str))
            else:
                lc_messages.append(SystemMessage(content=content_str))
                
        return lc_messages

# Convenience function
@asynccontextmanager
async def create_mcp_client(config: Optional[Config] = None):
    """
    Create an MCP client session using the provided configuration.
    """
    client = MCPAdapterClient(config)
    async with client.session():
        yield client