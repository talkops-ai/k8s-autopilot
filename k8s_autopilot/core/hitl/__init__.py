"""
Human-in-the-Loop (HITL) module for K8s Autopilot.

This module provides:
- Custom interrupt gates for phase-level approvals
- HITL middleware for tool-level approvals
- Checkpointer configuration and management
- Utilities for approval status management
"""

from k8s_autopilot.core.hitl.checkpointer import (
    create_checkpointer,
    get_checkpointer,
    CheckpointerType
)
from k8s_autopilot.core.hitl.utils import (
    create_approval_status,
    format_review_data,
    build_interrupt_payload,
    update_approval_status,
    is_approved
)
from k8s_autopilot.core.hitl.gates import (
    planning_review_gate,
    generation_review_gate
)
from k8s_autopilot.core.hitl.middleware import (
    create_hitl_middleware_config,
    create_supervisor_with_hitl,
    add_hitl_to_existing_agent,
    is_hitl_middleware_available,
    get_tool_names,
    DEFAULT_HITL_TOOLS
)
from k8s_autopilot.core.hitl.gate_tools import (
    request_planning_review,
    request_generation_review,
    create_hitl_gate_tools
)
from k8s_autopilot.core.hitl.client_helpers import (
    create_resume_command,
    extract_interrupt_data,
    format_interrupt_for_display,
    validate_resume_decision
)
from k8s_autopilot.core.hitl.tool_interrupt_helpers import (
    request_tool_clarification,
    request_tool_approval,
    request_tool_input
)
from k8s_autopilot.core.hitl.request_validator import (
    is_helm_related_request,
    validate_and_reject_non_helm
)
from k8s_autopilot.core.hitl.validation_tool import validate_request_scope

__all__ = [
    # Checkpointer
    "create_checkpointer",
    "get_checkpointer",
    "CheckpointerType",
    # Utilities
    "create_approval_status",
    "format_review_data",
    "build_interrupt_payload",
    "update_approval_status",
    "is_approved",
    # Gates
    "planning_review_gate",
    "generation_review_gate",
    # Middleware
    "create_hitl_middleware_config",
    "create_supervisor_with_hitl",
    "add_hitl_to_existing_agent",
    "is_hitl_middleware_available",
    "get_tool_names",
    "DEFAULT_HITL_TOOLS",
    # Gate Tools
    "request_planning_review",
    "request_generation_review",
    "create_hitl_gate_tools",
    # Client Helpers
    "create_resume_command",
    "extract_interrupt_data",
    "format_interrupt_for_display",
    "validate_resume_decision",
    # Tool Interrupt Helpers
    "request_tool_clarification",
    "request_tool_approval",
    "request_tool_input",
    # Request Validation
    "is_helm_related_request",
    "validate_and_reject_non_helm",
    "validate_request_scope",
]

