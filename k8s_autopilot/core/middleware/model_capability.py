"""
Model Capability Registry — provider-agnostic reasoning effort mapping.

Provides a centralized capability registry and reasoning effort translation
layer for all LLM providers used by k8s-autopilot deep agents.

Key features:
  - ``ModelCapabilities`` dataclass declares per-provider/model feature flags
  - ``ModelCapabilityRegistry`` maps provider+model → capabilities
  - ``map_reasoning_params()`` translates generic "low/medium/high" effort
    to provider-specific kwargs (OpenAI, Anthropic, Google GenAI, etc.)

Usage::

    from k8s_autopilot.core.middleware.model_capability import (
        ModelCapabilityRegistry,
        get_capability_registry,
    )

    registry = get_capability_registry()
    caps = registry.get("openai", "o4-mini")
    params = registry.map_reasoning_params("anthropic", "high")
    # → {"thinking": {"type": "enabled", "budget_tokens": 16384}}

Reference: dcode's configurable model + init_chat_model patterns.
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = AgentLogger("ModelCapability")


# ---------------------------------------------------------------------------
# Model Capabilities
# ---------------------------------------------------------------------------

@dataclass
class ModelCapabilities:
    """Feature flags for a specific LLM model/provider combination.

    Used by middleware to guard against unsupported operations (e.g.
    dropping system messages for models that don't support them,
    skipping image inputs for text-only models).
    """

    supports_system_message: bool = True
    supports_images: bool = True
    supports_tool_calls: bool = True
    supports_thinking: bool = False
    max_input_tokens: int = 128_000
    max_output_tokens: int = 16_384
    unsupported_modalities: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reasoning Effort Mapping
# ---------------------------------------------------------------------------

def _effort_to_budget(effort: str) -> int:
    """Map generic effort level to a token budget for thinking/reasoning."""
    return {
        "low": 1024,
        "medium": 4096,
        "high": 16384,
    }.get(effort.lower(), 4096)


def _openai_reasoning_params(effort: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """OpenAI o-series reasoning effort mapping.

    OpenAI's o1/o3/o4-mini models accept ``reasoning_effort`` directly.
    """
    return {"reasoning_effort": effort.lower()}


def _anthropic_reasoning_params(effort: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """Anthropic Claude thinking configuration.

    Claude 3.5+ uses ``thinking`` dict with ``type`` and ``budget_tokens``.
    """
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": _effort_to_budget(effort),
        }
    }


def _google_genai_reasoning_params(effort: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """Google GenAI / Gemini thinking configuration.

    Supports both Gemini 2.5 (thinking_budget) and Gemini 3.x/3.5 (thinking_level).
    """
    effort_lower = effort.lower()
    thinking_level = effort_lower if effort_lower in ("low", "medium", "high") else "medium"
    
    is_gemini_2_5 = False
    if model_name and "2.5" in model_name:
        is_gemini_2_5 = True
        
    if is_gemini_2_5:
        return {
            "thinking_config": {
                "thinking_budget": _effort_to_budget(effort),
            }
        }
    else:
        return {
            "thinking_config": {
                "thinking_level": thinking_level,
            }
        }


def _ollama_reasoning_params(effort: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """Ollama thinking configuration.

    DeepSeek-R1, QwQ, etc. use ``think=True``.
    """
    return {"think": True}


# Provider → reasoning params factory
REASONING_EFFORT_MAP: Dict[str, Callable[[str, Optional[str]], Dict[str, Any]]] = {
    "openai": _openai_reasoning_params,
    "azure_openai": _openai_reasoning_params,
    "anthropic": _anthropic_reasoning_params,
    "bedrock": _anthropic_reasoning_params,
    "aws_bedrock": _anthropic_reasoning_params,
    "bedrock_converse": _anthropic_reasoning_params,
    "google_genai": _google_genai_reasoning_params,
    "gemini": _google_genai_reasoning_params,
    "ollama": _ollama_reasoning_params,
}


# ---------------------------------------------------------------------------
# Capability Registry
# ---------------------------------------------------------------------------

class ModelCapabilityRegistry:
    """Central registry of LLM model capabilities and reasoning effort mapping.

    Provides:
      - ``get(provider, model)`` → ``ModelCapabilities`` feature flags
      - ``map_reasoning_params(provider, effort)`` → provider-specific kwargs
      - ``register(provider, model, caps)`` → custom capability registration

    The registry ships with sensible defaults per provider. Per-model overrides
    can be registered at runtime for fine-grained control.
    """

    # Provider-level defaults (used when no model-specific override exists)
    _PROVIDER_DEFAULTS: Dict[str, ModelCapabilities] = {
        "openai": ModelCapabilities(
            supports_thinking=True,
            max_input_tokens=128_000,
        ),
        "azure_openai": ModelCapabilities(
            supports_thinking=True,
            max_input_tokens=128_000,
        ),
        "anthropic": ModelCapabilities(
            supports_thinking=True,
            max_input_tokens=200_000,
        ),
        "google_genai": ModelCapabilities(
            supports_thinking=True,
            max_input_tokens=1_000_000,
        ),
        "gemini": ModelCapabilities(
            supports_thinking=True,
            max_input_tokens=1_000_000,
        ),
        "ollama": ModelCapabilities(
            supports_images=False,
            supports_tool_calls=False,
            supports_thinking=False,
            max_input_tokens=32_000,
        ),
        "fireworks": ModelCapabilities(
            supports_thinking=False,
            max_input_tokens=128_000,
        ),
        "openrouter": ModelCapabilities(
            supports_thinking=False,
            max_input_tokens=128_000,
        ),
    }

    def __init__(self) -> None:
        # model-specific overrides: "provider:model" → capabilities
        self._model_caps: Dict[str, ModelCapabilities] = {}

    def register(
        self,
        provider: str,
        model: str,
        caps: ModelCapabilities,
    ) -> None:
        """Register model-specific capabilities (overrides provider defaults)."""
        key = f"{provider}:{model}"
        self._model_caps[key] = caps
        logger.debug(f"Registered capabilities for {key}")

    def get(
        self,
        provider: str,
        model: str = "",
    ) -> ModelCapabilities:
        """Look up capabilities for a provider/model combination.

        Resolution order:
          1. Exact ``provider:model`` match
          2. Provider-level default
          3. Global default (all features enabled)
        """
        # Model-specific override
        key = f"{provider}:{model}"
        if key in self._model_caps:
            return self._model_caps[key]

        # Provider-level default
        norm_provider = provider.lower().replace("-", "_")
        if norm_provider in self._PROVIDER_DEFAULTS:
            return self._PROVIDER_DEFAULTS[norm_provider]

        # Global fallback
        return ModelCapabilities()

    def map_reasoning_params(
        self,
        provider: str,
        effort: Optional[str],
        model_name: Optional[str] = None,
     ) -> Dict[str, Any]:
        """Translate generic reasoning effort → provider-specific kwargs.

        Args:
            provider: LLM provider name (e.g. "openai", "anthropic").
            effort: Generic effort level ("low", "medium", "high") or None.
            model_name: Optional model name to refine parameters (e.g. Gemini 2.5 vs 3.x).

        Returns:
            Dict of provider-specific kwargs to merge into model invocation.
            Empty dict if effort is None or provider is unknown.
        """
        if not effort:
            return {}

        norm_provider = provider.lower().replace("-", "_")
        factory = REASONING_EFFORT_MAP.get(norm_provider)
        if factory is None:
            logger.debug(
                f"No reasoning effort mapping for provider {provider!r} — "
                f"ignoring effort={effort!r}"
            )
            return {}

        return factory(effort, model_name)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_registry: Optional[ModelCapabilityRegistry] = None


def get_capability_registry() -> ModelCapabilityRegistry:
    """Return the global ModelCapabilityRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = ModelCapabilityRegistry()
    return _registry
