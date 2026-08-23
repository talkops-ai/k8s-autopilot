"""
Model result metadata — structured return type from ``create_model()``.

Adapted from dcode's ``ModelResult`` pattern: the model factory returns
not just a raw ``BaseChatModel`` but also provider, context-limit, and
modality metadata. This lets coordinators and middleware make informed
decisions without re-introspecting the model at every step.

Reference: dcode/code/config.py  ModelResult dataclass
"""

from __future__ import annotations

import importlib
import importlib.util
from k8s_autopilot.utils.logger import AgentLogger
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = AgentLogger("ModelResult")


# ---------------------------------------------------------------------------
# ModelSpec — typed, validated "provider:model" pair
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    """A model specification in ``provider:model`` format.

    Adapted from dcode's ``ModelSpec`` — immutable, validated dataclass
    for parsing ``"anthropic:claude-sonnet-4-5"`` style strings.

    Examples::

        >>> spec = ModelSpec.parse("openai:gpt-4o")
        >>> spec.provider
        'openai'
        >>> spec.model
        'gpt-4o'
        >>> str(spec)
        'openai:gpt-4o'
    """

    provider: str
    """The provider name (e.g., ``'anthropic'``, ``'openai'``)."""

    model: str
    """The model identifier (e.g., ``'claude-sonnet-4-5'``, ``'gpt-4o'``)."""

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("Provider cannot be empty")
        if not self.model:
            raise ValueError("Model cannot be empty")

    @classmethod
    def parse(cls, spec: str) -> "ModelSpec":
        """Parse a ``'provider:model'`` string.

        Args:
            spec: Model specification (e.g., ``'anthropic:claude-sonnet-4-5'``).

        Returns:
            Parsed ModelSpec.

        Raises:
            ValueError: If spec is not in valid format.
        """
        if ":" not in spec:
            raise ValueError(
                f"Invalid model spec '{spec}': must be in provider:model format "
                "(e.g., 'anthropic:claude-sonnet-4-5')"
            )
        provider, model = spec.split(":", 1)
        return cls(provider=provider.strip(), model=model.strip())

    @classmethod
    def try_parse(cls, spec: str) -> Optional["ModelSpec"]:
        """Non-raising variant of ``parse()``."""
        try:
            return cls.parse(spec)
        except ValueError:
            return None

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


# ---------------------------------------------------------------------------
# ModelResult — structured return from create_model()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelResult:
    """Result of model creation — the model plus its metadata.

    Adapted from dcode's ``ModelResult``. Coordinators and middleware use
    this to access context limits, provider identity, and modality support
    without re-introspecting the model.

    Attributes:
        model: The ready-to-use LangChain chat model.
        model_name: Model identifier (e.g., ``'gpt-4o'``).
        provider: Resolved provider name (e.g., ``'openai'``).
        context_limit: Max input tokens from the model's profile, or None.
        unsupported_modalities: Frozenset of modalities the model doesn't
            support (e.g., ``{'audio', 'video'}``).
    """

    model: "BaseChatModel"
    model_name: str
    provider: str
    context_limit: Optional[int] = None
    unsupported_modalities: frozenset[str] = field(
        default_factory=frozenset
    )

    def __str__(self) -> str:
        return f"{self.provider}:{self.model_name}"

    @property
    def spec(self) -> str:
        """Return the ``provider:model`` spec string."""
        return f"{self.provider}:{self.model_name}"


# ---------------------------------------------------------------------------
# Provider credential registry
# ---------------------------------------------------------------------------

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_CLOUD_PROJECT",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "litellm": "LITELLM_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
}
"""Well-known providers → API key env var name.

Used by ``has_provider_credentials()`` to fail fast with an actionable
message rather than letting the SDK raise an opaque auth error.
"""

# Providers that support ambient auth (e.g., Vertex AI ADC) —
# skip credential checks for these.
IMPLICIT_AUTH_PROVIDERS: frozenset[str] = frozenset({
    "google_vertexai",
    "bedrock",
    "aws_bedrock",
    "bedrock_converse",
    "ollama",
})

# Provider base URL env vars (for gateway/proxy support)
PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
    "ollama": "OLLAMA_BASE_URL",
    "litellm": "LITELLM_BASE_URL",
}


def has_provider_credentials(provider: str) -> bool | None:
    """Check whether credentials are configured for a provider.

    Args:
        provider: Provider name.

    Returns:
        ``True`` if credentials exist, ``False`` if missing,
        ``None`` if the provider is unknown (can't determine).
    """
    import os

    if provider in IMPLICIT_AUTH_PROVIDERS:
        return True

    env_var = PROVIDER_API_KEY_ENV.get(provider)
    if env_var is None:
        return None  # Unknown provider

    value = os.environ.get(env_var, "").strip()
    return bool(value)


def get_credential_env_var(provider: str) -> str | None:
    """Return the API key env var name for a provider, or None."""
    return PROVIDER_API_KEY_ENV.get(provider)


# ---------------------------------------------------------------------------
# Provider profile discovery
# ---------------------------------------------------------------------------

_provider_profiles_cache: dict[str, dict[str, Any]] = {}


def _load_provider_profiles(provider: str) -> dict[str, Any]:
    """Load ``_PROFILES`` from a LangChain provider's data module.

    Adapted from dcode's ``_load_provider_profiles()``. Uses
    ``importlib.util`` to locate the package without triggering heavy
    imports, then loads only the ``_profiles.py`` file.

    Args:
        provider: Provider name (e.g., ``'openai'``).

    Returns:
        The ``_PROFILES`` dict from the provider's profile module,
        or an empty dict if unavailable.
    """
    if provider in _provider_profiles_cache:
        return _provider_profiles_cache[provider]

    # Map provider → package name
    package_name = f"langchain_{provider}"
    module_path = f"{package_name}.data._profiles"

    try:
        spec = importlib.util.find_spec(package_name)
        if spec is None:
            _provider_profiles_cache[provider] = {}
            return {}

        if spec.origin:
            package_dir = Path(spec.origin).parent
        elif spec.submodule_search_locations:
            package_dir = Path(next(iter(spec.submodule_search_locations)))
        else:
            _provider_profiles_cache[provider] = {}
            return {}

        profiles_path = package_dir / "data" / "_profiles.py"
        if not profiles_path.exists():
            _provider_profiles_cache[provider] = {}
            return {}

        file_spec = importlib.util.spec_from_file_location(
            module_path, profiles_path
        )
        if file_spec is None or file_spec.loader is None:
            _provider_profiles_cache[provider] = {}
            return {}

        module = importlib.util.module_from_spec(file_spec)
        file_spec.loader.exec_module(module)
        profiles = getattr(module, "_PROFILES", {})
        _provider_profiles_cache[provider] = profiles
        return profiles

    except Exception:
        logger.debug(f"Failed to load profiles for {provider}", exc_info=True)
        _provider_profiles_cache[provider] = {}
        return {}


def get_model_profile(
    provider: str,
    model_name: str,
) -> dict[str, Any] | None:
    """Get profile data for a specific model.

    Returns:
        Profile dict with keys like ``max_input_tokens``, ``tool_calling``,
        ``image_inputs`` etc., or None if unavailable.
    """
    profiles = _load_provider_profiles(provider)
    return profiles.get(model_name)


def get_context_limit(
    provider: str,
    model_name: str,
) -> int | None:
    """Return the model's max input tokens, or None if unknown."""
    profile = get_model_profile(provider, model_name)
    if profile and isinstance(profile.get("max_input_tokens"), int):
        return profile["max_input_tokens"]
    return None


def get_unsupported_modalities(
    provider: str,
    model_name: str,
) -> frozenset[str]:
    """Return modalities the model does NOT support."""
    profile = get_model_profile(provider, model_name)
    if not profile:
        return frozenset()

    modality_keys = {
        "image_inputs": "image",
        "audio_inputs": "audio",
        "video_inputs": "video",
        "pdf_inputs": "pdf",
    }
    return frozenset(
        label for key, label in modality_keys.items()
        if profile.get(key) is False
    )


# ---------------------------------------------------------------------------
# Model capability validation
# ---------------------------------------------------------------------------

def validate_model_capabilities(
    provider: str,
    model_name: str,
) -> list[str]:
    """Validate that a model has required capabilities for deep agents.

    Adapted from dcode's ``validate_model_capabilities()``.

    Returns:
        List of warning strings. Empty if all checks pass.
    """
    warnings: list[str] = []
    profile = get_model_profile(provider, model_name)

    if profile is None:
        warnings.append(
            f"No capability profile for '{provider}:{model_name}'. "
            "Cannot verify tool calling support."
        )
        return warnings

    # Check tool_calling
    if profile.get("tool_calling") is False:
        warnings.append(
            f"Model '{provider}:{model_name}' does not support tool calling. "
            "Deep Agents requires tool calling for agent functionality."
        )

    # Warn on small context windows
    max_tokens = profile.get("max_input_tokens")
    if max_tokens and max_tokens < 8000:
        warnings.append(
            f"Model '{provider}:{model_name}' has limited context "
            f"({max_tokens:,} tokens). Agent performance may be affected."
        )

    return warnings


# ---------------------------------------------------------------------------
# Retry-param resolution
# ---------------------------------------------------------------------------

# Provider → retry kwarg name mapping
_RETRY_PARAM_NAMES: dict[str, str] = {
    "openai": "max_retries",
    "anthropic": "max_retries",
    "google_genai": "max_retries",
    "azure_openai": "max_retries",
    "fireworks": "max_retries",
    "together": "max_retries",
    "groq": "max_retries",
    "ollama": "max_retries",
}


def resolve_retry_param_name(provider: str) -> str:
    """Return the retry-count kwarg name for a provider.

    Most providers use ``max_retries``; custom providers may differ.
    """
    return _RETRY_PARAM_NAMES.get(provider, "max_retries")
