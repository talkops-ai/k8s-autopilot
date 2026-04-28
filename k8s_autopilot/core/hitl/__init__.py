"""HITL (Human-in-the-Loop) module — consolidated.

Exports:
    - create_hitl_tools(): Tools for supervisor/coordinator
    - get_checkpointer(): Checkpointer factory
"""

from k8s_autopilot.core.hitl.tools import create_hitl_tools, request_human_input
from k8s_autopilot.core.hitl.checkpointer import get_checkpointer

__all__ = ["create_hitl_tools", "request_human_input", "get_checkpointer"]
