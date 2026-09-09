"""Model module for K8s Autopilot."""

from k8s_autopilot.model.config import ModelConfig, ModelSpec
from k8s_autopilot.model.factory import (
    ModelResult,
    create_model,
    detect_provider,
    normalize_model_spec,
)

__all__ = [
    "ModelConfig",
    "ModelResult",
    "ModelSpec",
    "create_model",
    "detect_provider",
    "normalize_model_spec",
]
