import os
import requests
from typing import Any, Dict, Optional
from langchain.tools import tool
from langchain_core.tools import StructuredTool
from langchain_core.runnables.config import RunnableConfig
from pydantic import BaseModel, Field


def get_config() -> Any:
    """Retrieve resolved Config from the central configuration engine."""
    from k8s_autopilot.config.config import Config
    return Config()


# 1. get_current_thread_id
@tool
def get_current_thread_id(config: RunnableConfig) -> str:
    """Get the current Deep Agents thread ID for LangSmith or MCP tooling.

    Args:
        config: Runtime config injected by LangChain.

    Returns:
        The current `configurable.thread_id`, or an explanatory message if missing.
    """
    thread_id = config.get("configurable", {}).get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    return "No current thread ID is available."


def create_get_current_thread_id_tool() -> Any:
    return get_current_thread_id


# 2. web_search (Tavily search)
class WebSearchInput(BaseModel):
    query: str = Field(description="The search query (be specific and detailed)")
    max_results: Optional[int] = Field(default=None, description="Number of results to return (default: resolved from config)")


def create_web_search_tool() -> Any:
    @tool(args_schema=WebSearchInput)
    def web_search(query: str, max_results: Optional[int] = None) -> str:
        """Search the web using Tavily for current information and documentation.

        Args:
            query: The search query
            max_results: Number of results
        """
        cfg = get_config()
        api_key = os.environ.get("TAVILY_API_KEY") or cfg.TAVILY_API_KEY
        if not api_key:
            return "Error: TAVILY_API_KEY not set in configuration or environment."

        limit = max_results if max_results is not None else cfg.TAVILY_MAX_RESULTS
        
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=api_key)
            response = client.search(query=query, max_results=limit)
            return str(response)
        except Exception as e:
            return f"Error during web search: {e}"

    return web_search


# 3. fetch_url
class FetchUrlInput(BaseModel):
    url: str = Field(description="The URL of the page to fetch")
    timeout: Optional[int] = Field(default=None, description="Request timeout in seconds (default: resolved from config)")


def create_fetch_url_tool() -> Any:
    @tool(args_schema=FetchUrlInput)
    def fetch_url(url: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Fetch URL content and convert it to markdown.

        Args:
            url: URL to fetch
            timeout: Timeout
        """
        cfg = get_config()
        t_out = timeout if timeout is not None else cfg.WEB_SEARCH_TIMEOUT

        try:
            from markdownify import markdownify as md_func
        except ImportError:
            def md_fallback(html: str, **kwargs: Any) -> str:
                return html
            md_func = md_fallback  # type: ignore[assignment]

        try:
            response = requests.get(
                url,
                timeout=t_out,
                headers={"User-Agent": "Mozilla/5.0 (compatible; DeepAgents/1.0)"}
            )
            markdown_content = md_func(response.text)
            return {
                "url": str(response.url),
                "markdown_content": markdown_content,
                "status_code": response.status_code,
                "content_length": len(markdown_content),
            }
        except Exception as e:
            return {"error": f"Fetch URL error: {e}", "url": url}

    return fetch_url
