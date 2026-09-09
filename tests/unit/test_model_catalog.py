"""Unit tests for Model Catalog, Profiles, Provider Detection, and Spec Normalization."""

from __future__ import annotations

import pytest

from k8s_autopilot.model.config import (
    AVAILABLE_MODELS,
    MODEL_PROFILES,
    ModelSpec,
    detect_provider,
    get_available_models_list,
    get_model_profile,
    get_provider_display_name,
    normalize_model_spec,
    resolve_model_spec,
)


class TestModelCatalog:
    """Test model catalog and profiles."""

    def test_model_spec_parsing(self):
        spec = ModelSpec.parse("anthropic:claude-3-7-sonnet")
        assert spec.provider == "anthropic"
        assert spec.model == "claude-3-7-sonnet"
        assert str(spec) == "anthropic:claude-3-7-sonnet"

        with pytest.raises(ValueError):
            ModelSpec.parse("invalid_no_colon")

        try_parsed = ModelSpec.try_parse("google_genai:gemini-3.7-flash")
        assert try_parsed is not None
        assert try_parsed.provider == "google_genai"

        assert ModelSpec.try_parse("invalid") is None

    def test_detect_provider(self):
        assert detect_provider("gemini-2.5-pro") == "google_genai"
        assert detect_provider("claude-3-7-sonnet") == "anthropic"
        assert detect_provider("claude-sonnet-5") == "anthropic"
        assert detect_provider("claude-opus-4-7") == "anthropic"
        assert detect_provider("gpt-4o") == "openai"
        assert detect_provider("gpt-5.5") == "openai"
        assert detect_provider("deepseek-chat") == "deepseek"
        assert detect_provider("grok-4.5") == "xai"
        assert detect_provider("mistral-large-latest") == "mistralai"
        assert detect_provider("sonar-pro") == "perplexity"
        assert detect_provider("command-r-plus") == "cohere"
        assert detect_provider("nemotron-3-ultra") == "nvidia"
        assert detect_provider("accounts/fireworks/models/glm-5p2") == "fireworks"
        assert detect_provider("llama-3.3-70b-versatile") == "ollama"

    def test_normalize_model_spec(self):
        assert normalize_model_spec("gemini-2.5-pro") == "google_genai:gemini-2.5-pro"
        assert normalize_model_spec("claude-3-7-sonnet") == "anthropic:claude-3-7-sonnet"
        assert normalize_model_spec("claude-sonnet-5") == "anthropic:claude-sonnet-5"
        assert normalize_model_spec("gpt-5.5") == "openai:gpt-5.5"
        assert normalize_model_spec("grok-4.5") == "xai:grok-4.5"
        assert normalize_model_spec("google_genai:gemini-2.5-pro") == "google_genai:gemini-2.5-pro"

    def test_resolve_model_spec(self):
        provider, model = resolve_model_spec("google_genai:gemini-3.7-flash")
        assert provider == "google_genai"
        assert model == "gemini-3.7-flash"

        provider, model = resolve_model_spec("claude-3-7-sonnet")
        assert provider == "anthropic"
        assert model == "claude-3-7-sonnet"

        provider, model = resolve_model_spec("grok-4.5")
        assert provider == "xai"
        assert model == "grok-4.5"

    def test_model_profiles(self):
        assert "google_genai:gemini-3.8-flash" in MODEL_PROFILES
        assert "google_vertexai:gemini-3.8-flash" in MODEL_PROFILES
        prof_38 = get_model_profile("google_genai:gemini-3.8-flash")
        assert prof_38 is not None
        assert prof_38["profile"]["reasoning_output"] is True

        prof = get_model_profile("google_genai:gemini-3.7-flash")
        assert prof is not None
        assert prof["profile"]["reasoning_output"] is True
        assert prof["profile"]["max_input_tokens"] == 1_000_000

        prof_claude = get_model_profile("anthropic:claude-3-7-sonnet")
        assert prof_claude is not None
        assert prof_claude["profile"]["reasoning_output"] is True

        prof_sonnet5 = get_model_profile("anthropic:claude-sonnet-5")
        assert prof_sonnet5 is not None
        assert prof_sonnet5["profile"]["tool_calling"] is True

        prof_gpt55 = get_model_profile("openai:gpt-5.5")
        assert prof_gpt55 is not None
        assert prof_gpt55["profile"]["reasoning_output"] is True

        prof_grok = get_model_profile("xai:grok-4.5")
        assert prof_grok is not None
        assert prof_grok["profile"]["tool_calling"] is True

    def test_available_models_list(self):
        models = get_available_models_list()
        assert len(models) > 0
        specs = [m[0] for m in models]
        # Anthropic modern models
        assert "anthropic:claude-3-7-sonnet" in specs
        assert "anthropic:claude-sonnet-4-5" in specs
        assert "anthropic:claude-sonnet-5" in specs
        assert "anthropic:claude-opus-4-7" in specs
        assert "anthropic:claude-opus-5" in specs
        assert "anthropic:claude-haiku-4-5" in specs

        # OpenAI modern models
        assert "openai:gpt-5.5" in specs
        assert "openai:gpt-5.4" in specs
        assert "openai:o3-mini" in specs
        assert "openai:gpt-4o" in specs

        # Other providers
        assert "google_genai:gemini-3.8-flash" in specs
        assert "google_genai:gemini-3.7-flash" in specs
        assert "google_vertexai:gemini-3.8-flash" in specs
        assert "google_vertexai:gemini-3.7-flash" in specs
        assert "fireworks:accounts/fireworks/models/glm-5p2" in specs
        assert "xai:grok-4.5" in specs
        assert "groq:llama-3.3-70b-versatile" in specs
        assert "together:meta-llama/Llama-3.3-70B-Instruct-Turbo" in specs
        assert "baseten:zai-org/GLM-5.2" in specs
        assert "bedrock:anthropic.claude-3-7-sonnet-20250219-v1:0" in specs

    def test_20_plus_providers_in_catalog(self):
        """Verify 20+ model providers are registered in AVAILABLE_MODELS."""
        assert len(AVAILABLE_MODELS) >= 20
        expected_providers = {
            "google_genai",
            "anthropic",
            "openai",
            "openrouter",
            "fireworks",
            "together",
            "xai",
            "mistralai",
            "groq",
            "deepseek",
            "cohere",
            "perplexity",
            "nvidia",
            "baseten",
            "azure_openai",
            "bedrock",
            "google_vertexai",
            "ibm",
            "huggingface",
            "ollama",
            "meta",
        }
        for p in expected_providers:
            assert p in AVAILABLE_MODELS, f"Provider {p} missing from AVAILABLE_MODELS"

    def test_provider_display_names(self):
        assert get_provider_display_name("google_genai") == "Google GenAI"
        assert get_provider_display_name("anthropic") == "Anthropic"
        assert get_provider_display_name("openai") == "OpenAI"
        assert get_provider_display_name("fireworks") == "Fireworks AI"
        assert get_provider_display_name("together") == "Together AI"
        assert get_provider_display_name("xai") == "xAI"
        assert get_provider_display_name("mistralai") == "Mistral AI"
        assert get_provider_display_name("baseten") == "Baseten"
        assert get_provider_display_name("bedrock") == "AWS Bedrock"
        assert get_provider_display_name("google_vertexai") == "Google Vertex AI"
