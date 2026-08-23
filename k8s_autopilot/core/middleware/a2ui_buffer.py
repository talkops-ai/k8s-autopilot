"""A2UI telemetry buffering middleware — prevents LLM context exhaustion."""

import json
from typing import Any
from langchain_core.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("A2UIBuffer")


@register_middleware(name="a2ui_buffer")
class A2UIBufferMiddleware(BaseAgentMiddleware):
    """Intercepts large A2UI JSON responses from MCP tools and buffers them in artifacts.
    
    Prevents LLM context exhaustion by removing the huge JSON payload from the
    text content that the model sees, replacing it with a pointer for build_obs_a2ui.
    """
    
    A2UI_TOOLS = {
        "prom_query_a2ui_chart": "metrics",
        "loki_query_a2ui": "logs",
        "tempo_query_a2ui": "traces",
        "otel_query_a2ui": "otel",
        "am_query_a2ui": "alerts"
    }

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        result = handler(request)
        return self._process_result(request, result)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        result = await handler(request)
        return self._process_result(request, result)

    def _process_result(self, request: ToolCallRequest, result: Any) -> Any:
        try:
            tool_name = request.tool_call.get("name") if isinstance(request.tool_call, dict) else request.tool_call.name
        except AttributeError:
            return result

        if tool_name not in self.A2UI_TOOLS:
            return result

        if not isinstance(result, ToolMessage):
            return result

        # 1. Skip buffering if LangChain marked the tool execution as an error
        if getattr(result, "is_error", False) or getattr(result, "status", "") == "error":
            return result

        try:
            content_str = result.content
            if isinstance(content_str, list):
                parts = []
                for block in content_str:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict) and "text" in block:
                        parts.append(str(block["text"]))
                    elif hasattr(block, "text"):
                        parts.append(str(getattr(block, "text")))
                    else:
                        parts.append(str(block))
                content_str = "".join(parts)

            # 2. Skip buffering if output is plain text (like an error message)
            data = json.loads(content_str)
            
            # 3. Skip buffering if the MCP server returned a valid JSON error object
            if isinstance(data, dict) and data.get("isError") or "error" in data:
                return result
            
            # Save the raw data into the artifact
            artifact = result.artifact or {}
            if isinstance(artifact, dict):
                artifact["a2ui_buffered_data"] = data
            else:
                artifact = {"original_artifact": artifact, "a2ui_buffered_data": data}
            result.artifact = artifact
            
            kind = self.A2UI_TOOLS[tool_name]
            
            # Replace the massive text content with a safe pointer string
            result.content = (
                f"Data successfully fetched and buffered in tool artifact. "
                f"Now call `build_obs_a2ui` with kind='{kind}' and data='__USE_ARTIFACT__' to render it."
            )
        except Exception as e:
            logger.debug(f"A2UIBufferMiddleware failed to parse JSON from {tool_name}: {e}")
            
        return result
