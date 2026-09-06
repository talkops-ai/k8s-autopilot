"""Reasoning effort support and provider-native thinking configuration.

Translates standard `reasoning_effort` ('low', 'medium', 'high', 'max')
into provider-native request shapes:
- Google GenAI (Gemini): `include_thoughts=True`, `thinking_level="HIGH"`, `thinking_budget=8192`
- Anthropic (Claude): `thinking={"type": "enabled", "budget_tokens": 8192}`
- OpenAI: `reasoning={"effort": "high"}`
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from k8s_autopilot.model.config import (
    MODEL_PROFILES,
    ModelProfile,
    ModelSpec,
    get_model_profile as _config_get_model_profile,
)

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_EFFORT_LEVELS = ("high", "medium", "low")


def get_model_profile(spec: str) -> dict[str, Any] | None:
    """Return model profile for spec with exact and prefix matching."""
    if not spec:
        return None

    # Check exact match
    if spec in MODEL_PROFILES:
        return dict(MODEL_PROFILES[spec])

    # Check prefix matching
    for key, prof in MODEL_PROFILES.items():
        if spec.startswith(key) or key.startswith(spec):
            return dict(prof)

    # Check via config helper
    entry = _config_get_model_profile(spec)
    if entry and entry.get("profile"):
        prof = entry["profile"]
        # If it was a generic fallback not in MODEL_PROFILES, return None for unknown models
        if spec not in MODEL_PROFILES and not any(spec.startswith(k) for k in MODEL_PROFILES):
            return None
        return dict(prof)

    return None


def _model_profile(
    model_spec: str | None, *, cli_override: dict[str, Any] | None = None
) -> Mapping[str, Any] | None:
    if not model_spec:
        return None
    profile = cli_override or get_model_profile(model_spec)
    if profile is None or not isinstance(profile, Mapping):
        return None
    if profile.get("reasoning_output") is not True:
        return None
    return profile


def supported_efforts_for_model(
    model_spec: str | None, *, cli_override: dict[str, Any] | None = None
) -> tuple[str, ...]:
    """Return the ordered reasoning effort levels supported by `model_spec`."""
    profile = _model_profile(model_spec, cli_override=cli_override)
    if profile is None or "reasoning_effort_levels" not in profile:
        return ("low", "medium", "high")
    levels = profile["reasoning_effort_levels"]
    if not isinstance(levels, list):
        return ("low", "medium", "high")
    return tuple(str(lvl) for lvl in levels)


def default_effort_for_model(
    model_spec: str | None, *, cli_override: dict[str, Any] | None = None
) -> str | None:
    """Return the profile's reasoning effort default independently of its levels."""
    profile = _model_profile(model_spec, cli_override=cli_override)
    if profile is None:
        return None
    default = profile.get("reasoning_effort_default")
    if default is not None:
        return str(default)
    return "medium"


def is_effort_supported_for_model(
    model_spec: str, effort: str, *, cli_override: dict[str, Any] | None = None
) -> bool:
    return effort.lower() in [
        e.lower() for e in supported_efforts_for_model(model_spec, cli_override=cli_override)
    ]


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _effort_paths(provider: str) -> tuple[tuple[str, ...], ...]:
    if provider == "openai":
        return (("reasoning", "effort"), ("reasoning_effort",))
    if provider == "anthropic":
        return (("effort",), ("reasoning_effort",), ("output_config", "effort"), ("thinking",))
    if provider == "google_genai":
        return (("thinking_level",), ("reasoning_effort",), ("thinking_config", "thinking_level"))
    return (("reasoning_effort",),)


def _path_is_present(model_params: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    if len(path) == 1:
        return path[0] in model_params
    nested = model_params.get(path[0])
    return isinstance(nested, Mapping) and path[1] in nested


def has_explicit_effort_model_params(
    model_spec: str | None, model_params: dict[str, Any] | None
) -> bool:
    """Return whether canonical or native effort parameters are present."""
    if not model_spec or not model_params:
        return False
    parsed = ModelSpec.try_parse(model_spec)
    provider = parsed.provider if parsed is not None else ""
    return any(_path_is_present(model_params, path) for path in _effort_paths(provider))


def current_effort_from_model_params(
    model_spec: str | None, model_params: dict[str, Any] | None
) -> str | None:
    """Read canonical or native effort settings using integration precedence."""
    if not model_spec or not model_params:
        return None
    parsed = ModelSpec.try_parse(model_spec)
    provider = parsed.provider if parsed is not None else ""

    if provider == "openai":
        reasoning = model_params.get("reasoning")
        if isinstance(reasoning, Mapping) and "effort" in reasoning:
            return _str_or_none(reasoning["effort"])
    elif provider == "anthropic" and "effort" in model_params:
        effort = model_params["effort"]
        if effort is not None:
            return _str_or_none(effort)
    elif provider == "google_genai" and "thinking_level" in model_params:
        effort = model_params["thinking_level"]
        if effort is not None:
            return _str_or_none(effort)

    return _str_or_none(model_params.get("reasoning_effort"))


def without_effort_model_params(
    model_spec: str | None, existing: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Remove canonical and native effort settings without changing siblings."""
    if not existing:
        return None
    cleaned = dict(existing)
    cleaned.pop("reasoning_effort", None)

    parsed = ModelSpec.try_parse(model_spec) if model_spec else None
    provider = parsed.provider if parsed is not None else ""
    if provider == "openai":
        cleaned.pop("reasoning", None)
    elif provider == "anthropic":
        cleaned.pop("thinking", None)
        cleaned.pop("effort", None)
    elif provider == "google_genai":
        cleaned.pop("thinking_level", None)
        cleaned.pop("thinking_budget", None)
        cleaned.pop("include_thoughts", None)
    return cleaned or None


def with_effort_model_params(
    model_spec: str | None, existing: dict[str, Any] | None, effort: str
) -> dict[str, Any]:
    """Inject provider-native thinking/reasoning parameters."""
    updated = without_effort_model_params(model_spec, existing) or {}
    updated["reasoning_effort"] = effort

    parsed = ModelSpec.try_parse(model_spec) if model_spec else None
    provider = parsed.provider if parsed is not None else ""
    eff_lower = effort.lower()

    if provider == "google_genai":
        valid_level = eff_lower if eff_lower in ("minimal", "low", "medium", "high") else "medium"
        updated["include_thoughts"] = True
        updated["thinking_level"] = valid_level
        updated["thinking_budget"] = {
            "low": 1024,
            "medium": 2048,
            "high": 8192,
            "max": 16384,
        }.get(eff_lower, 4096)
    elif provider == "anthropic":
        budget = {
            "low": 1024,
            "medium": 4096,
            "high": 8192,
            "max": 16384,
        }.get(eff_lower, 4096)
        updated["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif provider == "openai":
        updated["reasoning"] = {"effort": eff_lower}

    return updated
