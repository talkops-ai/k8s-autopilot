"""LLM Model construction factory and provider detection.

Builds dynamic chat model instances for LangChain agents based on the
active model spec (``provider:model``) and reasoning effort configuration.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.language_models import BaseChatModel

from k8s_autopilot.config.settings import get_settings, resolve_env_var
from k8s_autopilot.exceptions import (
    MissingCredentialsError,
    MissingProviderPackageError,
    ModelConfigError,
    NoCredentialsConfiguredError,
)
from k8s_autopilot.model.config import (
    IMPLICIT_AUTH_PROVIDERS,
    ModelConfig,
    ModelSpec,
    apply_stored_credentials,
    detect_provider,
    get_credential_env_var,
    get_model_profile,
    has_provider_credentials,
    normalize_model_spec,
)

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModelResult:
    """Chat model instance with associated metadata."""

    model: BaseChatModel
    model_name: str
    provider: str
    context_limit: int | None = None
    unsupported_modalities: frozenset[str] = frozenset()

    def apply_to_settings(self) -> None:
        """Commit this result's metadata to the global settings singleton."""
        s = get_settings()
        s.model = self.model_name
        s.model_name = self.model_name
        s.model_provider = self.provider
        s.model_context_limit = self.context_limit
        s.model_unsupported_modalities = self.unsupported_modalities


def _get_default_model_spec() -> str:
    """Resolve the default model spec to use based on settings or configured credentials."""
    from k8s_autopilot.config.settings import _load_dotenv

    _load_dotenv(refresh_loaded=True)
    config = ModelConfig.load()
    settings = get_settings()

    def _has_creds(spec: str) -> bool:
        provider = spec.split(":", 1)[0] if ":" in spec else detect_provider(spec)
        return bool(provider and has_provider_credentials(provider))

    # 1. If explicit model set in settings and has credentials
    if settings.model_name and _has_creds(settings.model_name):
        return normalize_model_spec(settings.model_name)
    if settings.model and _has_creds(settings.model):
        return normalize_model_spec(settings.model)

    # 2. If default / recent model set in config and has credentials
    if config.default_model and _has_creds(config.default_model):
        return config.default_model
    if config.recent_model and _has_creds(config.recent_model):
        return config.recent_model

    # 3. Auto-detect from available credentials in provider priority order
    for prov, default_model in (
        ("google_genai", "google_genai:gemini-2.5-flash"),
        ("anthropic", "anthropic:claude-3-7-sonnet"),
        ("openai", "openai:gpt-4o"),
        ("groq", "groq:llama-3.3-70b-versatile"),
        ("deepseek", "deepseek:deepseek-chat"),
        ("openrouter", "openrouter:anthropic/claude-3.5-sonnet"),
    ):
        if has_provider_credentials(prov) is True:
            return default_model

    # 4. If explicit model set without credentials (e.g. testing / local provider)
    if settings.model_name:
        return normalize_model_spec(settings.model_name)
    if settings.model:
        return normalize_model_spec(settings.model)

    raise NoCredentialsConfiguredError(
        "No credentials configured for any provider. Configure your API key in Settings."
    )


def _get_provider_kwargs(provider: str, *, model_name: str | None = None) -> dict[str, Any]:
    """Retrieve all configuration arguments for the provider/model."""
    config = ModelConfig.load()
    result = config.get_kwargs(provider, model_name=model_name)

    base_url = config.get_base_url(provider)
    if base_url:
        result["base_url"] = base_url

    settings = get_settings()
    env_var = get_credential_env_var(provider)
    api_key = None
    if env_var:
        api_key = resolve_env_var(env_var)

    if not api_key:
        field_map = {
            "openai": "openai_api_key",
            "anthropic": "anthropic_api_key",
            "google_genai": "google_api_key",
            "google": "google_api_key",
            "groq": "groq_api_key",
            "deepseek": "deepseek_api_key",
            "openrouter": "openrouter_api_key",
            "mistralai": "mistral_api_key",
            "mistral": "mistral_api_key",
            "fireworks": "fireworks_api_key",
            "together": "together_api_key",
            "xai": "xai_api_key",
            "cohere": "cohere_api_key",
            "perplexity": "perplexity_api_key",
            "nvidia": "nvidia_api_key",
            "huggingface": "huggingface_api_key",
            "bedrock": "aws_access_key_id",
            "azure_openai": "azure_openai_api_key",
        }
        field = field_map.get(provider)
        if field and hasattr(settings, field):
            api_key = getattr(settings, field)

    if not api_key and provider in ("google_genai", "google"):
        api_key = resolve_env_var("GEMINI_API_KEY") or getattr(settings, "google_api_key", None)

    if api_key:
        result["api_key"] = api_key
        if provider in ("google_genai", "google"):
            result["google_api_key"] = api_key
        if env_var:
            os.environ[env_var] = str(api_key)

    # Vertex AI handling
    if provider in ("google_genai", "google"):
        use_vertex_env = resolve_env_var("GOOGLE_GENAI_USE_VERTEXAI") or str(
            getattr(settings, "google_genai_use_vertexai", False)
        )
        is_vertex = str(use_vertex_env).strip().lower() in ("true", "1", "yes")
        result["vertexai"] = is_vertex
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true" if is_vertex else "false"

        project_env = resolve_env_var("GOOGLE_CLOUD_PROJECT") or getattr(settings, "google_cloud_project", None)
        if project_env:
            result["project"] = project_env
            os.environ["GOOGLE_CLOUD_PROJECT"] = str(project_env)
        location_env = resolve_env_var("GOOGLE_CLOUD_LOCATION") or getattr(settings, "google_cloud_location", None)
        if location_env:
            result["location"] = location_env
            os.environ["GOOGLE_CLOUD_LOCATION"] = str(location_env)

    # Reasoning effort injection
    from k8s_autopilot.model.reasoning import with_effort_model_params

    effort = getattr(settings, "reasoning_effort", None)
    if effort:
        spec = f"{provider}:{model_name}" if model_name else provider
        result = with_effort_model_params(spec, result, effort)

    return result


def _create_model_from_class(
    class_path: str,
    model_name: str,
    provider: str,
    kwargs: dict[str, Any],
) -> BaseChatModel:
    """Instantiate a chat model dynamically via class reflection."""
    if ":" not in class_path:
        raise ModelConfigError(f"Invalid class_path '{class_path}': must be module:Class format")

    module_path, class_name = class_path.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ModelConfigError(f"Module '{module_path}' not found for class_path '{class_path}': {e}") from e

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ModelConfigError(f"Class '{class_name}' not found in module '{module_path}'")

    if not (isinstance(cls, type) and issubclass(cls, BaseChatModel)):
        raise ModelConfigError(f"'{class_path}' is not a BaseChatModel subclass")

    cls_kwargs = dict(kwargs)
    if provider in ("google_genai", "google", "anthropic"):
        cls_kwargs.pop("reasoning_effort", None)

    cls_any = cast(Any, cls)
    try:
        return cls_any(model=model_name, **cls_kwargs)
    except TypeError:
        return cls_any(model_name=model_name, **cls_kwargs)


def _create_model_via_init(
    model_name: str,
    provider: str,
    kwargs: dict[str, Any],
) -> BaseChatModel:
    """Delegate to langchain's dynamic init_chat_model function."""
    from langchain.chat_models import init_chat_model

    init_kwargs = dict(kwargs)
    if provider in ("google_genai", "google", "anthropic"):
        init_kwargs.pop("reasoning_effort", None)

    try:
        if provider:
            return init_chat_model(model_name, model_provider=provider, **init_kwargs)
        return init_chat_model(model_name, **init_kwargs)
    except ImportError as e:
        package_map = {
            "anthropic": "langchain-anthropic",
            "openai": "langchain-openai",
            "google_genai": "langchain-google-genai",
            "groq": "langchain-groq",
            "deepseek": "langchain-deepseek",
        }
        package = package_map.get(provider, f"langchain-{provider}")
        raise MissingProviderPackageError(
            f"Missing package for '{provider}'. Install: pip install {package}",
            provider=provider,
            package=package,
        ) from e
    except (ValueError, TypeError) as e:
        raise ModelConfigError(f"Invalid configuration for '{provider}:{model_name}': {e}") from e


def create_model(
    model_spec: str | None = None,
    *,
    extra_kwargs: dict[str, Any] | None = None,
    profile_overrides: dict[str, Any] | None = None,
) -> ModelResult:
    """Factory function to build a BaseChatModel and return ModelResult."""
    if not model_spec:
        model_spec = _get_default_model_spec()

    parsed = ModelSpec.try_parse(model_spec)
    if parsed:
        provider = parsed.provider
        model_name = parsed.model
    else:
        model_name = model_spec
        provider = detect_provider(model_spec) or ""

    if provider:
        apply_stored_credentials(provider)

    if provider and provider not in IMPLICIT_AUTH_PROVIDERS:
        cred_status = has_provider_credentials(provider)
        if not cred_status:
            env_var = get_credential_env_var(provider)
            raise MissingCredentialsError(
                f"No credentials configured for provider '{provider}'. Set environment variable {env_var} or configure it in Settings.",
                provider=provider,
                env_var=env_var,
            )

    kwargs = _get_provider_kwargs(provider, model_name=model_name)
    if extra_kwargs:
        kwargs.update(extra_kwargs)
        eff = extra_kwargs.get("reasoning_effort")
        if eff:
            from k8s_autopilot.model.reasoning import with_effort_model_params

            spec = f"{provider}:{model_name}" if model_name else (model_name or "")
            kwargs = with_effort_model_params(spec, kwargs, eff)

    config = ModelConfig.load()
    class_path = config.get_class_path(provider) if provider else None

    logger.info(
        "Instantiating ChatModel '%s' (provider=%s)",
        model_name,
        provider,
        extra={
            "model": model_name,
            "provider": provider,
            "reasoning_effort": kwargs.get("reasoning_effort", "medium"),
            "temperature": kwargs.get("temperature", 0.0),
        },
    )

    if class_path:
        model = _create_model_from_class(class_path, model_name, provider, kwargs)
    else:
        model = _create_model_via_init(model_name, provider, kwargs)

    # Resolve model profile & context limits
    spec_key = f"{provider}:{model_name}" if provider else model_name
    prof_entry = get_model_profile(spec_key)
    profile: dict[str, Any] = dict(prof_entry["profile"]) if prof_entry else {}
    if profile_overrides:
        profile.update(cast(dict[str, Any], profile_overrides))

    context_limit = profile.get("max_input_tokens")
    unsupported_modalities = frozenset(profile.get("unsupported_modalities", []))

    return ModelResult(
        model=model,
        model_name=model_name,
        provider=provider or getattr(model, "_model_provider", ""),
        context_limit=context_limit,
        unsupported_modalities=unsupported_modalities,
    )
