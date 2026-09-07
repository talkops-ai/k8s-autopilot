"""Constants shared across K8s Autopilot modules."""

from __future__ import annotations

from typing import Final

SYSTEM_MESSAGE_PREFIX: Final = "[K8s Autopilot]"
"""Prefix for internal system messages injected by middleware."""

FS_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
)
"""Standard filesystem tools."""

READONLY_FS_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "view_file",
        "list_dir",
        "dir_list",
        "grep_search",
        "file_search",
        "read_url_content",
        "fetch_web_page",
        "search_web",
        "get_goal",
        "get_rubric",
        "propose_goal",
        "update_goal",
        "write_todos",
    }
)
"""Read-only and inspection tools exempt from HITL gating by default."""
