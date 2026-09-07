"""Model configuration structures, catalogs, and helpers for LLM providers.

Matches the OpsCode model system:
- Supports all providers: Google GenAI, Anthropic, OpenAI, OpenRouter, Groq, DeepSeek, Ollama, etc.
- Detailed capabilities: reasoning_output, tool_calling, max_tokens, etc.
- Dynamic spec resolution: ``provider:model`` format
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import os
from types import MappingProxyType
from typing import Any, TypedDict, cast

from k8s_autopilot.config.settings import get_settings, resolve_env_var
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_CLOUD_PROJECT",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "ibm": "WATSONX_APIKEY",
    "litellm": "LITELLM_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PPLX_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
}

PROVIDER_BASE_URL_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"),
    "azure_openai": ("AZURE_OPENAI_ENDPOINT",),
    "baseten": ("BASETEN_BASE_URL", "BASETEN_API_BASE"),
    "cohere": ("CO_API_URL",),
    "deepseek": ("DEEPSEEK_API_BASE",),
    "fireworks": ("FIREWORKS_BASE_URL", "FIREWORKS_API_BASE"),
    "google_genai": ("GOOGLE_GEMINI_BASE_URL",),
    "groq": ("GROQ_BASE_URL", "GROQ_API_BASE"),
    "huggingface": ("HF_INFERENCE_ENDPOINT",),
    "ibm": ("WATSONX_URL",),
    "meta": ("MODEL_API_BASE",),
    "mistralai": ("MISTRAL_BASE_URL",),
    "nvidia": ("NVIDIA_BASE_URL",),
    "openai": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    "openrouter": ("OPENROUTER_API_BASE",),
    "perplexity": ("PERPLEXITY_BASE_URL",),
    "together": ("TOGETHER_API_BASE",),
    "xai": ("XAI_API_BASE",),
}

IMPLICIT_AUTH_PROVIDERS: set[str] = {"google_vertexai", "bedrock"}
NO_AUTH_REQUIRED_PROVIDERS: set[str] = {"ollama"}
OPTIONAL_AUTH_ENV: dict[str, str] = {"ollama": "OLLAMA_API_KEY"}


class ProviderAuthState(StrEnum):
    """Authentication state for a single LLM provider credential."""

    CONFIGURED = "configured"
    MISSING = "missing"
    IMPLICIT = "implicit"
    NOT_REQUIRED = "not_required"
    MANAGED = "managed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderAuthStatus:
    """Aggregate authentication status across all configured providers."""

    state: str
    provider: str
    env_var: str | None = None
    detail: str = ""

    def as_legacy_bool(self) -> bool | None:
        """Convert the auth state into a legacy boolean representation.

        Returns:
            True if authenticated or managed, False if missing, or None if unknown.
        """
        if self.state in (
            ProviderAuthState.CONFIGURED,
            ProviderAuthState.IMPLICIT,
            ProviderAuthState.NOT_REQUIRED,
            ProviderAuthState.MANAGED,
        ):
            return True
        if self.state == ProviderAuthState.MISSING:
            return False
        return None


@dataclass(frozen=True)
class ModelSpec:
    """Parsed model specification with provider, model name, and parameters."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("Provider cannot be empty")
        if not self.model:
            raise ValueError("Model cannot be empty")

    @classmethod
    def parse(cls, spec: str) -> ModelSpec:
        """Parse a model specification string into a ModelSpec instance.

        Args:
            spec: String in 'provider:model' format.

        Returns:
            ModelSpec instance.

        Raises:
            ValueError: If spec does not contain ':' or components are empty.
        """
        if ":" not in spec:
            raise ValueError(f"Invalid spec '{spec}': must be provider:model format")
        provider, model = spec.split(":", 1)
        return cls(provider=provider.strip(), model=model.strip())

    @classmethod
    def try_parse(cls, spec: str) -> ModelSpec | None:
        """Attempt to parse a model specification string without raising an exception.

        Args:
            spec: String in 'provider:model' format.

        Returns:
            ModelSpec instance if valid, None otherwise.
        """
        try:
            return cls.parse(spec)
        except ValueError:
            return None

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


class ModelProfile(TypedDict, total=False):
    """Capability profile for a specific model (context window, features, pricing)."""

    name: str
    max_input_tokens: int
    max_output_tokens: int
    text_inputs: bool
    image_inputs: bool
    audio_inputs: bool
    pdf_inputs: bool
    video_inputs: bool
    reasoning_output: bool
    reasoning_effort_levels: list[str]
    reasoning_effort_default: str
    tool_calling: bool
    structured_output: bool
    status: str | None


class ModelProfileEntry(TypedDict):
    """Single entry in the model profile registry mapping spec to capabilities."""

    profile: ModelProfile
    overridden_keys: set[str]


class ProviderConfig(TypedDict, total=False):
    """Configuration for an LLM provider (API keys, base URLs, overrides)."""

    enabled: bool
    models: list[str]
    api_key_env: str
    base_url: str
    base_url_env: str
    class_path: str
    params: dict[str, Any]
    profile: dict[str, Any]
    display_name: str


@dataclass(frozen=True)
class ModelConfig:
    """Complete model configuration including all providers and their profiles."""

    default_model: str | None = None
    recent_model: str | None = None
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.providers, MappingProxyType):
            object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))

    @classmethod
    def load(cls) -> ModelConfig:
        """Load the active model configuration from application settings.

        Returns:
            ModelConfig initialized with default and recent model selections.
        """
        settings = get_settings()
        default = getattr(settings, "model", None) or "gemini-3.7-flash"
        return cls(default_model=default, recent_model=default)

    def is_provider_enabled(self, provider: str) -> bool:
        """Check whether a model provider is enabled in configuration.

        Args:
            provider: Provider name (e.g. 'openai', 'anthropic').

        Returns:
            True if enabled; False otherwise.
        """
        provider_config = self.providers.get(provider)
        if provider_config is None:
            return True
        return provider_config.get("enabled", True)

    def get_kwargs(self, provider: str, *, model_name: str | None = None) -> dict[str, Any]:
        """Retrieve model initialization keyword arguments for a provider.

        Args:
            provider: Provider identifier.
            model_name: Optional specific model name for model-specific overrides.

        Returns:
            Dictionary of provider kwargs.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return {}
        params = dict(provider_config.get("params", {}))
        if model_name and isinstance(params.get(model_name), dict):
            model_params = params.pop(model_name)
            params = {k: v for k, v in params.items() if not isinstance(v, dict)}
            params.update(model_params)
        else:
            params = {k: v for k, v in params.items() if not isinstance(v, dict)}
        return params

    def get_base_url(self, provider: str) -> str | None:
        """Resolve the effective base URL for a provider from env or config.

        Args:
            provider: Provider identifier.

        Returns:
            Resolved base URL string or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        base_url_env = provider_config.get("base_url_env")
        if base_url_env:
            val = resolve_env_var(base_url_env)
            if val:
                return val
        return provider_config.get("base_url")

    def get_base_url_env(self, provider: str) -> str | None:
        """Get the configured base URL environment variable name for a provider.

        Args:
            provider: Provider identifier.

        Returns:
            Environment variable name or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        return provider_config.get("base_url_env")

    def get_api_key_env(self, provider: str) -> str | None:
        """Get the configured API key environment variable name for a provider.

        Args:
            provider: Provider identifier.

        Returns:
            Environment variable name or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        return provider_config.get("api_key_env")

    def get_class_path(self, provider: str) -> str | None:
        """Get the custom class path configured for a provider's model wrapper.

        Args:
            provider: Provider identifier.

        Returns:
            Python import path string or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        return provider_config.get("class_path")

    def get_profile_overrides(self, provider: str, *, model_name: str | None = None) -> dict[str, Any]:
        """Retrieve profile capability overrides for a provider or model.

        Args:
            provider: Provider identifier.
            model_name: Optional specific model name.

        Returns:
            Dictionary of profile overrides.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return {}
        profile = dict(provider_config.get("profile", {}))
        if model_name and isinstance(profile.get(model_name), dict):
            model_profile = profile.pop(model_name)
            profile = {k: v for k, v in profile.items() if not isinstance(v, dict)}
            profile.update(model_profile)
        else:
            profile = {k: v for k, v in profile.items() if not isinstance(v, dict)}
        return profile


def get_credential_env_var(provider: str) -> str | None:
    """Return the primary environment variable name for a provider's API key.

    Args:
        provider: Provider identifier.

    Returns:
        Environment variable name or None.
    """
    return PROVIDER_API_KEY_ENV.get(provider)


def get_base_url_env_vars(provider: str) -> tuple[str, ...]:
    """Return candidate environment variable names for a provider's base URL.

    Args:
        provider: Provider identifier.

    Returns:
        Tuple of candidate environment variable names.
    """
    return PROVIDER_BASE_URL_ENV.get(provider, ())


PROVIDER_SETTINGS_FIELD_MAP: dict[str, str] = {
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

PROVIDER_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "google_genai": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "mistralai": ("MISTRAL_API_KEY", "MISTRALAI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY", "MISTRALAI_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"),
    "perplexity": ("PPLX_API_KEY", "PERPLEXITY_API_KEY"),
}


def get_provider_auth_status(provider: str) -> ProviderAuthStatus:
    """Inspect and resolve authentication status for a provider.

    Args:
        provider: Provider identifier.

    Returns:
        ProviderAuthStatus indicating current configuration state and credentials.
    """
    if provider in NO_AUTH_REQUIRED_PROVIDERS:
        return ProviderAuthStatus(state=ProviderAuthState.NOT_REQUIRED, provider=provider, detail="local provider")

    if provider in IMPLICIT_AUTH_PROVIDERS:
        return ProviderAuthStatus(state=ProviderAuthState.IMPLICIT, provider=provider, detail="implicit auth")

    settings = get_settings()
    env_var = get_credential_env_var(provider)

    val = None
    if env_var:
        val = resolve_env_var(env_var)

    if not val and provider in PROVIDER_KEY_ALIASES:
        for alias in PROVIDER_KEY_ALIASES[provider]:
            val = resolve_env_var(alias)
            if val:
                break

    if not val:
        field = PROVIDER_SETTINGS_FIELD_MAP.get(provider)
        if field and hasattr(settings, field):
            val = getattr(settings, field)

    if val:
        return ProviderAuthStatus(state=ProviderAuthState.CONFIGURED, provider=provider, env_var=env_var)

    optional_env = OPTIONAL_AUTH_ENV.get(provider)
    if optional_env and resolve_env_var(optional_env):
        return ProviderAuthStatus(state=ProviderAuthState.CONFIGURED, provider=provider, env_var=optional_env)

    if not env_var:
        return ProviderAuthStatus(state=ProviderAuthState.UNKNOWN, provider=provider, detail="credentials unknown")

    return ProviderAuthStatus(
        state=ProviderAuthState.MISSING, provider=provider, env_var=env_var, detail=f"{env_var} is not set"
    )


def has_provider_credentials(provider: str) -> bool | None:
    """Determine whether credentials are available for a given provider.

    Args:
        provider: Provider identifier.

    Returns:
        True if credentials exist, False if missing, None if indeterminate.
    """
    return get_provider_auth_status(provider).as_legacy_bool()


def apply_stored_credentials(provider: str) -> bool:
    """Bridge credentials into process env vars for LangChain model factories across all providers."""
    settings = get_settings()
    env_var = get_credential_env_var(provider)
    applied = False

    val = None
    if env_var:
        val = resolve_env_var(env_var)

    if not val and provider in PROVIDER_KEY_ALIASES:
        for alias in PROVIDER_KEY_ALIASES[provider]:
            val = resolve_env_var(alias)
            if val:
                break

    if not val:
        field = PROVIDER_SETTINGS_FIELD_MAP.get(provider)
        if field and hasattr(settings, field):
            val = getattr(settings, field)

    if val:
        if env_var:
            os.environ[env_var] = str(val)
        if provider in PROVIDER_KEY_ALIASES:
            for alias in PROVIDER_KEY_ALIASES[provider]:
                os.environ[alias] = str(val)
        applied = True

    if provider in ("google_genai", "google"):
        use_vertex = resolve_env_var("GOOGLE_GENAI_USE_VERTEXAI") or str(
            getattr(settings, "google_genai_use_vertexai", False)
        )
        if str(use_vertex).strip().lower() in ("true", "1", "yes"):
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            applied = True
        proj = resolve_env_var("GOOGLE_CLOUD_PROJECT") or getattr(settings, "google_cloud_project", None)
        if proj:
            os.environ["GOOGLE_CLOUD_PROJECT"] = str(proj)
        loc = resolve_env_var("GOOGLE_CLOUD_LOCATION") or getattr(settings, "google_cloud_location", None)
        if loc:
            os.environ["GOOGLE_CLOUD_LOCATION"] = str(loc)

    return applied


# ── Model Profiles & Catalog ─────────────────────────────

MODEL_PROFILES: dict[str, ModelProfile] = {
    # OpenRouter
    "openrouter:moonshotai/kimi-k3": {
        "name": "Kimi K3",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 1_000_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:anthropic/claude-sonnet-5": {
        "name": "Claude Sonnet 5",
        "max_input_tokens": 200_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:anthropic/claude-opus-4.7": {
        "name": "Claude Opus 4.7",
        "max_input_tokens": 200_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:google/gemini-3.6-flash": {
        "name": "Gemini 3.6 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    # OpenAI
    "openai:gpt-5.4": {
        "name": "GPT-5.4",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-5.4-mini": {
        "name": "GPT-5.4 mini",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-4o": {
        "name": "GPT-4o",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:o3-mini": {
        "name": "o3 Mini",
        "max_input_tokens": 200_000,
        "max_output_tokens": 100_000,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    # Anthropic
    "anthropic:claude-3-7-sonnet": {
        "name": "Claude 3.7 Sonnet",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-sonnet-latest": {
        "name": "Claude 3.5 Sonnet",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-opus-4-7": {
        "name": "Claude Opus 4.7",
        "max_input_tokens": 200_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    # Google GenAI
    "google_genai:gemini-3.7-flash": {
        "name": "Gemini 3.7 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.6-flash": {
        "name": "Gemini 3.6 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.5-flash": {
        "name": "Gemini 3.5 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.1-pro": {
        "name": "Gemini 3.1 Pro",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "high"],
        "reasoning_effort_default": "low",
        "tool_calling": True,
        "structured_output": True,
    },
    # Groq & DeepSeek
    "groq:llama-3.3-70b-versatile": {
        "name": "Llama 3.3 70B",
        "max_input_tokens": 128_000,
        "max_output_tokens": 32_768,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "deepseek:deepseek-chat": {
        "name": "DeepSeek V3",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "deepseek:deepseek-reasoner": {
        "name": "DeepSeek R1",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
}

AVAILABLE_MODELS: dict[str, list[tuple[str, str]]] = {
    "google_genai": [
        ("gemini-3.7-flash", "Gemini 3.7 Flash"),
        ("gemini-3.6-flash", "Gemini 3.6 Flash"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
        ("gemini-3.1-pro", "Gemini 3.1 Pro"),
    ],
    "anthropic": [
        ("claude-3-7-sonnet", "Claude 3.7 Sonnet (Thinking)"),
        ("claude-3-5-sonnet-latest", "Claude 3.5 Sonnet"),
        ("claude-opus-4-7", "Claude Opus 4.7 (Thinking)"),
    ],
    "openai": [
        ("gpt-5.4", "GPT-5.4"),
        ("gpt-5.4-mini", "GPT-5.4 mini"),
        ("gpt-4o", "GPT-4o"),
        ("o3-mini", "o3 Mini"),
    ],
    "openrouter": [
        ("google/gemini-3.6-flash", "Gemini 3.6 Flash"),
        ("anthropic/claude-sonnet-5", "Claude Sonnet 5"),
        ("anthropic/claude-opus-4.7", "Claude Opus 4.7"),
        ("moonshotai/kimi-k3", "Kimi K3"),
    ],
    "groq": [
        ("llama-3.3-70b-versatile", "Llama 3.3 70B"),
    ],
    "deepseek": [
        ("deepseek-chat", "DeepSeek V3"),
        ("deepseek-reasoner", "DeepSeek R1 (Reasoning)"),
    ],
    "ollama": [
        ("llama3.1", "Llama 3.1"),
        ("deepseek-r1:14b", "DeepSeek R1 14B"),
    ],
}

PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "google_genai": "Google GenAI",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "ollama": "Ollama",
    "azure_openai": "Azure OpenAI",
}


def get_provider_display_name(provider: str) -> str:
    """Return the user-facing display name for a model provider.

    Args:
        provider: Provider identifier.

    Returns:
        Formatted human-readable display name.
    """
    return PROVIDER_DISPLAY_NAMES.get(provider, provider.title())


def get_model_profile(spec: str) -> ModelProfileEntry | None:
    """Resolve capability and configuration profile for a model specification.

    Args:
        spec: Model specification string (e.g. 'openai:gpt-4o' or 'gpt-4o').

    Returns:
        ModelProfileEntry dictionary containing context limits, features, and defaults.
    """
    if ":" in spec:
        provider, model_id = spec.split(":", 1)
    else:
        provider, model_id = detect_provider(spec) or "google_genai", spec
        spec = f"{provider}:{model_id}"

    base_profile = dict(MODEL_PROFILES.get(spec, {}))
    if not base_profile:
        base_profile = {
            "name": model_id,
            "max_input_tokens": 128_000,
            "max_output_tokens": 16_384,
            "text_inputs": True,
            "image_inputs": False,
            "reasoning_output": False,
            "tool_calling": True,
            "structured_output": True,
        }

    return {"profile": cast(ModelProfile, base_profile), "overridden_keys": set()}


def get_available_models_list() -> list[tuple[str, str, str]]:
    """Return flat list of (spec, display_name, provider) tuples."""
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for provider, models in AVAILABLE_MODELS.items():
        for model_id, display_name in models:
            spec = f"{provider}:{model_id}"
            if spec not in seen:
                result.append((spec, display_name, provider))
                seen.add(spec)
    return result


def detect_provider(model_name: str) -> str | None:
    """Infer provider from model name prefixes."""
    name_lower = model_name.lower()
    if name_lower.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if name_lower.startswith(("claude-", "claude", "sonnet", "opus", "haiku")):
        return "anthropic"
    if name_lower.startswith("gemini"):
        return "google_genai"
    if name_lower.startswith("deepseek"):
        return "deepseek"
    if name_lower.startswith("groq"):
        return "groq"
    if name_lower.startswith("llama"):
        return "ollama"
    return None


def normalize_model_spec(model_spec: str) -> str:
    """Normalize a model specifier into `provider:model` format if bare."""
    if not model_spec:
        return model_spec
    if ":" in model_spec:
        return model_spec
    provider = detect_provider(model_spec)
    if provider:
        return f"{provider}:{model_spec}"
    return model_spec


def resolve_model_spec(spec: str | None) -> tuple[str, str]:
    """Resolve a model spec into (provider, model_id) tuple."""
    if not spec:
        spec = "google_genai:gemini-3.7-flash"
    if ":" in spec:
        provider, model_id = spec.split(":", 1)
        return provider.lower(), model_id
    provider = detect_provider(spec) or "google_genai"
    return provider, spec
