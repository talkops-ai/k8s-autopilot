"""Web search tool utilizing the Tavily API."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from langchain_core.tools import tool

from k8s_autopilot.config.settings import get_settings

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_tavily_client = None


def _get_tavily_client() -> Any:
    global _tavily_client
    if _tavily_client is not None:
        return _tavily_client

    settings = get_settings()
    api_key = getattr(settings, "tavily_api_key", None) or os.environ.get("TAVILY_API_KEY")
    if api_key:
        try:
            from tavily import TavilyClient

            _tavily_client = TavilyClient(api_key=api_key)
        except ImportError:
            logger.warning("tavily-python package is not installed.")

    return _tavily_client


@tool
def web_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
) -> dict[str, Any]:
    """Search the web using Tavily for current information and documentation.

    This tool searches the web and returns relevant results. After receiving results,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        query: The search query (be specific and detailed)
        max_results: Number of results to return (default: 5)
        topic: Search topic type - "general" for most queries, "news" for current events
        include_raw_content: Include full page content (warning: uses more tokens)

    Returns:
        Dictionary containing:
        - results: List of search results, each with:
            - title: Page title
            - url: Page URL
            - content: Relevant excerpt from the page
            - score: Relevance score (0-1)
        - query: The original search query
    """
    try:
        import requests
        from tavily import (
            BadRequestError,
            InvalidAPIKeyError,
            MissingAPIKeyError,
            UsageLimitExceededError,
        )
        from tavily.errors import ForbiddenError, TimeoutError as TavilyTimeoutError
    except ImportError as exc:
        return {"error": f"Required package not installed: {exc.name}."}

    client = _get_tavily_client()
    if client is None:
        return {
            "error": "Tavily API key not configured. Please set TAVILY_API_KEY environment variable.",
            "query": query,
        }

    try:
        return client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
    except (
        requests.exceptions.RequestException,
        ValueError,
        TypeError,
        BadRequestError,
        ForbiddenError,
        InvalidAPIKeyError,
        MissingAPIKeyError,
        TavilyTimeoutError,
        UsageLimitExceededError,
    ) as e:
        return {"error": f"Web search error: {e!s}", "query": query}
