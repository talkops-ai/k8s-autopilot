"""Prompt slot resolvers for k8s-autopilot."""

from k8s_autopilot.core.prompts.slots.mode import get_mode_slots
from k8s_autopilot.core.prompts.slots.model_identity import get_model_identity_slots
from k8s_autopilot.core.prompts.slots.environment import get_environment_slots

__all__ = [
    "get_mode_slots",
    "get_model_identity_slots",
    "get_environment_slots",
]
