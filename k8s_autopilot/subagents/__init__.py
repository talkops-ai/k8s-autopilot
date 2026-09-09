"""Subagents module for K8s Autopilot.

Provides subagent metadata types, filesystem discovery, and multi-tier loading.
"""

from k8s_autopilot.subagents.loader import (
    get_built_in_subagents,
    list_subagents,
    load_async_subagents,
    parse_subagent_file,
)
from k8s_autopilot.subagents.subagents_parser import (
    parse_built_in_subagents,
    parse_subagent_bundle,
)
from k8s_autopilot.subagents.types import SubagentMetadata

__all__ = [
    "SubagentMetadata",
    "get_built_in_subagents",
    "list_subagents",
    "load_async_subagents",
    "parse_built_in_subagents",
    "parse_subagent_bundle",
    "parse_subagent_file",
]
