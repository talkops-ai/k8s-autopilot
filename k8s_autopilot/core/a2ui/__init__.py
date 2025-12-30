# A2UI Support Module for K8s Autopilot
# Provides programmatic A2UI building for agent responses

from .schema import A2UI_SCHEMA
from .examples import K8S_UI_EXAMPLES
from .a2ui_builder import (
    build_working_status_a2ui,
    build_hitl_approval_a2ui,
    build_info_message_a2ui,
    build_completion_a2ui,
    build_error_a2ui,
    build_a2ui_for_response,
)

__all__ = [
    "A2UI_SCHEMA",
    "K8S_UI_EXAMPLES",
    # Builder functions
    "build_working_status_a2ui",
    "build_hitl_approval_a2ui",
    "build_info_message_a2ui",
    "build_completion_a2ui",
    "build_error_a2ui",
    "build_a2ui_for_response",
]
