"""Reasoning effort support and provider-native thinking configuration.

Translates standard `reasoning_effort` ('low', 'medium', 'high', 'max')
into provider-native request shapes:
- Google GenAI (Gemini): `include_thoughts=True`, `thinking_level="HIGH"`, `thinking_budget=8192`
- Anthropic (Claude): `thinking={"type": "enabled", "budget_tokens": 8192}`
- OpenAI: `reasoning={"effort": "high"}`
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from k8s_autopilot.model.config import (
    MODEL_PROFILES,
    ModelSpec,
    detect_provider,
    get_model_profile as _config_get_model_profile,
    normalize_model_spec,
)
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_EFFORT_LEVELS = ("none", "off", "minimal", "low", "medium", "high", "xhigh", "max")


def _resolve_provider(model_spec: str | None) -> str:
    """Resolve provider name from model spec string, supporting bare or prefixed formats."""
    if not model_spec:
        return ""
    parsed = ModelSpec.try_parse(model_spec)
    if parsed is not None and parsed.provider:
        return parsed.provider
    return detect_provider(model_spec) or ""


def get_model_profile(spec: str) -> dict[str, Any] | None:
    """Return model profile for spec with exact and prefix matching."""
    if not spec:
        return None

    normalized = normalize_model_spec(spec)

    # Check exact match
    if normalized in MODEL_PROFILES:
        return dict(MODEL_PROFILES[normalized])
    if spec in MODEL_PROFILES:
        return dict(MODEL_PROFILES[spec])

    # Check prefix matching
    for key, prof in MODEL_PROFILES.items():
        if normalized.startswith(key) or key.startswith(normalized) or spec.startswith(key) or key.startswith(spec):
            return dict(prof)

    # Check via config helper
    entry = _config_get_model_profile(normalized) or _config_get_model_profile(spec)
    if entry and entry.get("profile"):
        prof = entry["profile"]
        if prof.get("reasoning_output") or "reasoning_effort_levels" in prof:
            return dict(prof)

    return None


def _model_profile(model_spec: str | None, *, cli_override: dict[str, Any] | None = None) -> Mapping[str, Any] | None:
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
        return ()
    levels = profile["reasoning_effort_levels"]
    if not isinstance(levels, list):
        return ()
    return tuple(str(lvl) for lvl in levels)


def default_effort_for_model(model_spec: str | None, *, cli_override: dict[str, Any] | None = None) -> str | None:
    """Return the profile's reasoning effort default independently of its levels."""
    profile = _model_profile(model_spec, cli_override=cli_override)
    if profile is None or "reasoning_effort_default" not in profile:
        return None
    default = profile.get("reasoning_effort_default")
    if default is not None:
        return str(default)
    return None


def is_effort_supported_for_model(model_spec: str, effort: str, *, cli_override: dict[str, Any] | None = None) -> bool:
    """Check whether a given reasoning effort level is supported by the specified model.

    Args:
        model_spec: Model specification string (e.g., 'openai:gpt-4o').
        effort: Reasoning effort name to check.
        cli_override: Optional override dictionary from CLI arguments.

    Returns:
        True if the effort level is supported; False otherwise.
    """
    eff_lower = effort.lower()
    if eff_lower in ("off", "none"):
        return True
    return eff_lower in [e.lower() for e in supported_efforts_for_model(model_spec, cli_override=cli_override)]


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _effort_paths(provider: str) -> tuple[tuple[str, ...], ...]:
    if provider in ("openai", "openai_codex", "azure_openai"):
        return (("reasoning", "effort"), ("reasoning_effort",))
    if provider == "anthropic":
        return (("effort",), ("reasoning_effort",), ("output_config", "effort"), ("thinking",))
    if provider in ("google_genai", "google", "google_vertexai"):
        return (("thinking_level",), ("reasoning_effort",), ("thinking_config", "thinking_level"), ("include_thoughts",))
    return (("reasoning_effort",),)


def _path_is_present(model_params: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    if len(path) == 1:
        return path[0] in model_params
    nested = model_params.get(path[0])
    return isinstance(nested, Mapping) and path[1] in nested


def has_explicit_effort_model_params(model_spec: str | None, model_params: dict[str, Any] | None) -> bool:
    """Return whether canonical or native effort parameters are present."""
    if not model_spec or not model_params:
        return False
    provider = _resolve_provider(model_spec)
    return any(_path_is_present(model_params, path) for path in _effort_paths(provider))


def current_effort_from_model_params(model_spec: str | None, model_params: dict[str, Any] | None) -> str | None:
    """Read canonical or native effort settings using integration precedence."""
    if not model_spec or not model_params:
        return None
    provider = _resolve_provider(model_spec)

    if provider in {"openai", "openai_codex", "azure_openai"}:
        reasoning = model_params.get("reasoning")
        if isinstance(reasoning, Mapping) and "effort" in reasoning:
            return _str_or_none(reasoning["effort"])
    elif provider == "anthropic":
        if "effort" in model_params and model_params["effort"] is not None:
            return _str_or_none(model_params["effort"])
        output_config = model_params.get("output_config")
        if isinstance(output_config, Mapping) and "effort" in output_config:
            return _str_or_none(output_config["effort"])
        thinking = model_params.get("thinking")
        if isinstance(thinking, Mapping):
            budget = thinking.get("budget_tokens")
            if budget:
                budget_map = {1024: "low", 2048: "low", 4096: "medium", 8192: "high", 16384: "max"}
                if budget in budget_map:
                    return budget_map[budget]
    elif provider in {"google_genai", "google", "google_vertexai"}:
        if "thinking_level" in model_params and model_params["thinking_level"] is not None:
            return _str_or_none(model_params["thinking_level"])
        thinking_config = model_params.get("thinking_config")
        if isinstance(thinking_config, Mapping) and "thinking_level" in thinking_config:
            return _str_or_none(thinking_config["thinking_level"])
    elif provider == "fireworks":
        mk = model_params.get("model_kwargs")
        if isinstance(mk, Mapping) and "reasoning_effort" in mk:
            return _str_or_none(mk["reasoning_effort"])
    elif provider == "xai":
        eb = model_params.get("extra_body")
        if isinstance(eb, Mapping) and "reasoning_effort" in eb:
            return _str_or_none(eb["reasoning_effort"])

    return _str_or_none(model_params.get("reasoning_effort"))


def _remove_nested_key(params: dict[str, Any], container: str, key: str) -> None:
    nested = params.get(container)
    if not isinstance(nested, Mapping):
        return
    remaining = dict(nested)
    remaining.pop(key, None)
    if remaining:
        params[container] = remaining
    else:
        params.pop(container, None)


def without_effort_model_params(model_spec: str | None, existing: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove canonical and native effort settings without changing siblings."""
    if not existing:
        return None
    cleaned = dict(existing)
    cleaned.pop("reasoning_effort", None)

    provider = _resolve_provider(model_spec)
    if provider in ("openai", "openai_codex", "azure_openai"):
        _remove_nested_key(cleaned, "reasoning", "effort")
    elif provider == "anthropic":
        cleaned.pop("effort", None)
        _remove_nested_key(cleaned, "output_config", "effort")
        cleaned.pop("thinking", None)
    elif provider in ("google_genai", "google", "google_vertexai"):
        cleaned.pop("thinking_level", None)
        cleaned.pop("thinking_budget", None)
        cleaned.pop("include_thoughts", None)
        _remove_nested_key(cleaned, "thinking_config", "thinking_level")
        _remove_nested_key(cleaned, "thinking_config", "thinking_budget")
    elif provider == "fireworks":
        _remove_nested_key(cleaned, "model_kwargs", "reasoning_effort")
    elif provider == "xai":
        _remove_nested_key(cleaned, "extra_body", "reasoning_effort")
    return cleaned or None


def with_effort_model_params(model_spec: str | None, existing: dict[str, Any] | None, effort: str) -> dict[str, Any]:
    """Inject provider-native thinking/reasoning parameters."""
    updated = without_effort_model_params(model_spec, existing) or {}
    updated["reasoning_effort"] = effort

    provider = _resolve_provider(model_spec)
    eff_lower = effort.lower()

    if provider in ("google_genai", "google", "google_vertexai"):
        if eff_lower in ("off", "none"):
            updated["include_thoughts"] = False
            updated["thinking_budget"] = 0
        else:
            valid_level = eff_lower if eff_lower in ("minimal", "low", "medium", "high") else "medium"
            updated["include_thoughts"] = True
            updated["thinking_level"] = valid_level
    elif provider == "anthropic":
        if eff_lower in ("off", "none"):
            updated.pop("thinking", None)
        else:
            budget = {
                "low": 1024,
                "medium": 4096,
                "high": 8192,
                "max": 16384,
            }.get(eff_lower, 4096)
            updated["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif provider in ("openai", "openai_codex", "azure_openai"):
        # For OpenAI, ChatOpenAI handles reasoning_effort -> reasoning={"effort": ...} natively.
        # Do NOT inject updated["reasoning"] = {"effort": ...} alongside reasoning_effort,
        # because having both triggers a bug in langchain-openai where reasoning_effort is
        # erroneously passed to AsyncResponses.create().
        pass

    return updated
