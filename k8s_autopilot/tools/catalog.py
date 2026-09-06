# pyright: reportAttributeAccessIssue=false, reportMissingImports=false
"""Catalog of built-in tools for K8s Autopilot.

Ported from ``reference/opscode/src/opscode/tools/catalog.py``.
"""

from __future__ import annotations

from typing import Any

from k8s_autopilot.tools.fetch_url import fetch_url
from k8s_autopilot.tools.goal_tools import get_goal, get_rubric, propose_goal, update_goal
from k8s_autopilot.tools.registry import ToolRegistry
from k8s_autopilot.tools.thread import get_current_thread_id
from k8s_autopilot.tools.web_search import web_search


def register_all_tools() -> None:
    """Register all available tools with the ToolRegistry."""
    registry = ToolRegistry.get_instance()

    # Core general-purpose tools
    registry.register("web_search", lambda **kwargs: web_search, category="web")
    registry.register("fetch_url", lambda **kwargs: fetch_url, category="web")
    registry.register("get_current_thread_id", lambda **kwargs: get_current_thread_id, category="system")

    # Goal and Rubric tools
    registry.register("get_rubric", lambda **kwargs: get_rubric, category="goal")
    registry.register("get_goal", lambda **kwargs: get_goal, category="goal")
    registry.register("update_goal", lambda **kwargs: update_goal, category="goal")
    registry.register("propose_goal", lambda **kwargs: propose_goal, category="goal")

