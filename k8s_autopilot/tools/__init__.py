"""Tools catalog and registry for K8s Autopilot."""

from __future__ import annotations

from k8s_autopilot.tools.catalog import register_all_tools
from k8s_autopilot.tools.display import (
    format_json_output,
    format_tool_error,
    format_tool_success,
)
from k8s_autopilot.tools.fetch_url import fetch_url
from k8s_autopilot.tools.goal_tools import get_goal, get_rubric, update_goal
from k8s_autopilot.tools.registry import ToolRegistry
from k8s_autopilot.tools.thread import get_current_thread_id
from k8s_autopilot.tools.web_search import web_search

__all__ = [
    "ToolRegistry",
    "fetch_url",
    "format_json_output",
    "format_tool_error",
    "format_tool_success",
    "get_current_thread_id",
    "get_goal",
    "get_rubric",
    "register_all_tools",
    "update_goal",
    "web_search",
]
