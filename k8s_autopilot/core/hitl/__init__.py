"""HITL (Human-in-the-Loop) module — consolidated.

Exports:
    - create_hitl_tools(): Tools for supervisor/coordinator
    - get_checkpointer(): Sync checkpointer factory
    - get_async_checkpointer(): Async checkpointer factory (preferred)
    - close_async_pool(): Cleanup for server shutdown
"""

from k8s_autopilot.core.hitl.tools import create_hitl_tools, request_human_input
from k8s_autopilot.core.hitl.checkpointer import (
    get_checkpointer,
    get_async_checkpointer,
    close_async_pool,
)

__all__ = [
    "create_hitl_tools",
    "request_human_input",
    "get_checkpointer",
    "get_async_checkpointer",
    "close_async_pool",
]
