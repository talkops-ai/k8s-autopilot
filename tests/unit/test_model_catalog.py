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
        assert detect_provider("gpt-4o") == "openai"
        assert detect_provider("deepseek-chat") == "deepseek"
        assert detect_provider("llama-3.3-70b-versatile") == "ollama"

    def test_normalize_model_spec(self):
        assert normalize_model_spec("gemini-2.5-pro") == "google_genai:gemini-2.5-pro"
        assert normalize_model_spec("claude-3-7-sonnet") == "anthropic:claude-3-7-sonnet"
        assert normalize_model_spec("google_genai:gemini-2.5-pro") == "google_genai:gemini-2.5-pro"

    def test_resolve_model_spec(self):
        provider, model = resolve_model_spec("google_genai:gemini-3.7-flash")
        assert provider == "google_genai"
        assert model == "gemini-3.7-flash"

        provider, model = resolve_model_spec("claude-3-7-sonnet")
        assert provider == "anthropic"
        assert model == "claude-3-7-sonnet"

    def test_model_profiles(self):
        prof = get_model_profile("google_genai:gemini-3.7-flash")
        assert prof is not None
        assert prof["profile"]["reasoning_output"] is True
        assert prof["profile"]["max_input_tokens"] == 1_000_000

        prof_claude = get_model_profile("anthropic:claude-3-7-sonnet")
        assert prof_claude is not None
        assert prof_claude["profile"]["reasoning_output"] is True

    def test_available_models_list(self):
        models = get_available_models_list()
        assert len(models) > 0
        specs = [m[0] for m in models]
        assert "google_genai:gemini-3.7-flash" in specs
        assert "anthropic:claude-3-7-sonnet" in specs
        assert "openai:gpt-4o" in specs

    def test_provider_display_names(self):
        assert get_provider_display_name("google_genai") == "Google GenAI"
        assert get_provider_display_name("anthropic") == "Anthropic"
        assert get_provider_display_name("openai") == "OpenAI"
