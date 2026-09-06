"""Prompts module for K8s Autopilot."""

import re

from k8s_autopilot.prompts.system import (
    build_model_identity_section,
    get_base_system_prompt,
)

# Regex to find the ### Model Identity block for ConfigurableModelMiddleware to patch
MODEL_IDENTITY_RE = re.compile(
    r"^### Model Identity.*?(?=\n### |\Z)",
    re.MULTILINE | re.DOTALL,
)
"""Regex to match and replace the model identity section in system prompts."""

__all__ = [
    "MODEL_IDENTITY_RE",
    "build_model_identity_section",
    "get_base_system_prompt",
]
