"""
LLM model factory for K8s Autopilot Agent.

Provides a single entry-point to instantiate any LLM tier from a config dict
produced by ``Config.llm_config``, ``Config.llm_higher_config``, etc.

Enhanced with dcode patterns:
  - Returns ``ModelResult`` metadata (not raw model)
  - Early credential validation with actionable error messages
  - ``ModelSpec`` parsing for ``"provider:model"`` strings
  - Provider profile discovery for context limits and modalities
  - ``class_path`` support for custom ``BaseChatModel`` subclasses
  - ``validate_model_capabilities()`` for tool-calling and context warnings

Reference: dcode/code/config.py create_model(), model_config.py
"""

from __future__ import annotations

import importlib
from k8s_autopilot.utils.logger import AgentLogger
import os
from typing import Any, Dict, Optional

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from k8s_autopilot.utils.model_result import (
    IMPLICIT_AUTH_PROVIDERS,
    ModelResult,
    ModelSpec,
    get_context_limit,
    get_credential_env_var,
    get_unsupported_modalities,
    has_provider_credentials,
    resolve_retry_param_name,
    validate_model_capabilities,
)

logger = AgentLogger("LLMFactory")


# ---------------------------------------------------------------------------
# Custom class_path support
# ---------------------------------------------------------------------------

def _create_model_from_class(
    class_path: str,
    model_name: str,
    provider: str,
    kwargs: Dict[str, Any],
) -> BaseChatModel:
    """Instantiate a ``BaseChatModel`` from a ``module.path:ClassName`` string.

    Adapted from dcode's ``_create_model_from_class()``. Enables config-file
    providers to bypass ``init_chat_model`` entirely by specifying a custom
    class.

    Args:
        class_path: Fully-qualified class (e.g., ``'my_pkg.models:CustomChat'``).
        model_name: Model name to pass as ``model`` kwarg.
        provider: Provider name for error messages.
        kwargs: Extra constructor kwargs.

    Returns:
        An instantiated ``BaseChatModel``.

    Raises:
        ValueError: If the class cannot be imported or instantiated.
    """
    if ":" not in class_path:
        raise ValueError(
            f"Invalid class_path '{class_path}' for provider '{provider}': "
            "must be in 'module.path:ClassName' format"
        )

    module_path, class_name = class_path.rsplit(":", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(
            f"Could not import module '{module_path}' for provider "
            f"'{provider}': {exc}"
        ) from exc

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(
            f"Class '{class_name}' not found in module '{module_path}' "
            f"for provider '{provider}'"
        )

    try:
        return cls(model=model_name, **kwargs)
    except Exception as exc:
        raise ValueError(
            f"Failed to instantiate {class_path} for '{provider}:{model_name}': {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Core model creation
# ---------------------------------------------------------------------------

def create_model_with_result(
    llm_config: Dict[str, Any],
    *,
    extra_kwargs: Dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> ModelResult:
    """Create a chat model instance and return full metadata.

    This is the enhanced version that returns ``ModelResult`` with
    provider, context limit, and modality information.

    Args:
        llm_config: Config dict from any ``Config.llm_*_config`` property.
        extra_kwargs: Additional kwargs merged on top (highest priority).
        max_retries: Override retry count. Resolved to the correct
            provider-specific kwarg name.

    Returns:
        ``ModelResult`` containing the model, provider, context limit,
        and modality information.

    Raises:
        ValueError: If credentials are missing or model creation fails.
    """
    kwargs = {k: v for k, v in llm_config.items() if k != "provider"}
    provider = llm_config.get("provider", "")

    # Extract model name from various kwarg shapes
    model_name = kwargs.get("model", "")
    if not model_name:
        model_name = kwargs.get("model_name", "")

    # Parse provider:model from the model string if present
    if ":" in model_name and "/" not in model_name:
        parsed = ModelSpec.try_parse(model_name)
        if parsed:
            # The model string already has a provider prefix for init_chat_model
            # Keep model_name as the short name for metadata
            model_name = parsed.model
            if not provider:
                provider = parsed.provider

    # ── Early credential validation ───────────────────────────────────
    if provider and provider not in IMPLICIT_AUTH_PROVIDERS:
        cred_status = has_provider_credentials(provider)
        if cred_status is False:
            env_var = get_credential_env_var(provider)
            display_env = env_var or f"<{provider} API key>"
            raise ValueError(
                f"No credentials found for provider '{provider}'. "
                f"Please set the {display_env} environment variable."
            )

    # ── Capability validation (warnings only) ─────────────────────────
    if provider and model_name:
        warnings = validate_model_capabilities(provider, model_name)
        for w in warnings:
            logger.warning(f"Model capability: {w}")

    # ── Merge extra kwargs ────────────────────────────────────────────
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    # ── Retry param resolution ────────────────────────────────────────
    if max_retries is not None and provider:
        retry_param = resolve_retry_param_name(provider)
        kwargs[retry_param] = max_retries

    # ── Check for class_path (custom BaseChatModel) ───────────────────
    class_path = kwargs.pop("class_path", None)
    if class_path:
        model = _create_model_from_class(
            class_path, model_name, provider, kwargs,
        )
    else:
        logger.info(f"init_chat_model model_name={model_name} provider={provider} kwargs={kwargs}")
        model = init_chat_model(**kwargs)

    # ── Resolve metadata from provider profiles ───────────────────────
    context_limit = None
    unsupported_modalities: frozenset[str] = frozenset()

    if provider and model_name:
        context_limit = get_context_limit(provider, model_name)
        unsupported_modalities = get_unsupported_modalities(
            provider, model_name,
        )

    # Fallback: try to read from model.profile if profiles didn't resolve
    if context_limit is None:
        profile = getattr(model, "profile", None)
        if isinstance(profile, dict):
            raw_limit = profile.get("max_input_tokens")
            if isinstance(raw_limit, int):
                context_limit = raw_limit

    resolved_provider = provider or _detect_provider_from_model(model)

    return ModelResult(
        model=model,
        model_name=model_name or str(getattr(model, "model_name", "unknown")),
        provider=resolved_provider,
        context_limit=context_limit,
        unsupported_modalities=unsupported_modalities,
    )


def create_model(
    llm_config: Dict[str, Any],
    *,
    extra_kwargs: Dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> BaseChatModel:
    """Create a chat model instance from a config dict.

    **Backward-compatible** — returns a raw ``BaseChatModel`` so all
    existing call-sites continue working without changes.

    For full metadata (context limit, provider, modalities), use
    ``create_model_with_result()`` instead.

    Args:
        llm_config: Config dict from any ``Config.llm_*_config`` property.
        extra_kwargs: Additional kwargs merged on top.
        max_retries: Override retry count.

    Returns:
        A ready-to-use ``BaseChatModel``.
    """
    result = create_model_with_result(
        llm_config, extra_kwargs=extra_kwargs, max_retries=max_retries,
    )
    return result.model


def _detect_provider_from_model(model: BaseChatModel) -> str:
    """Try to detect the provider from the model's runtime metadata."""
    try:
        ls_params = model._get_ls_params()  # type: ignore[attr-defined]
        if isinstance(ls_params, dict):
            provider = ls_params.get("ls_provider")
            if isinstance(provider, str):
                return provider
    except (AttributeError, TypeError, RuntimeError, NotImplementedError):
        pass
    return ""


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

initialize_llm_model = create_model
initialize_llm_higher = create_model
initialize_llm_deepagent = create_model
