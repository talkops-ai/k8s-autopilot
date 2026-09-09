"""Unit tests for reasoning effort and provider-native thinking level transformation."""

from __future__ import annotations

import pytest

from k8s_autopilot.model.reasoning import (
    _remove_nested_key,
    current_effort_from_model_params,
    default_effort_for_model,
    has_explicit_effort_model_params,
    is_effort_supported_for_model,
    supported_efforts_for_model,
    with_effort_model_params,
    without_effort_model_params,
)


class TestModelReasoning:
    """Test thinking level and effort injection across supported providers."""

    def test_supported_efforts(self):
        efforts = supported_efforts_for_model("google_genai:gemini-3.7-flash")
        assert "high" in efforts
        assert "medium" in efforts
        assert "low" in efforts

    def test_is_effort_supported(self):
        assert is_effort_supported_for_model("google_genai:gemini-3.7-flash", "high") is True
        assert is_effort_supported_for_model("anthropic:claude-3-7-sonnet", "low") is True
        assert is_effort_supported_for_model("openai:gpt-5.5-pro", "xhigh") is True
        assert is_effort_supported_for_model("openai:gpt-5.5-pro", "off") is True
        assert is_effort_supported_for_model("openai:gpt-5.5-pro", "none") is True

    def test_google_genai_thinking_params(self):
        spec = "google_genai:gemini-3.7-flash"
        params = with_effort_model_params(spec, {}, "high")
        assert params["reasoning_effort"] == "high"
        # Google GenAI requires include_thoughts=True to return thoughts in output stream
        assert params["include_thoughts"] is True
        assert params["thinking_level"] == "high"

    def test_anthropic_thinking_params(self):
        spec = "anthropic:claude-3-7-sonnet"
        params = with_effort_model_params(spec, {}, "high")
        assert params["reasoning_effort"] == "high"
        # Claude extended thinking requires thinking parameter dict
        assert params["thinking"] == {"type": "enabled", "budget_tokens": 8192}

    def test_openai_reasoning_params(self):
        spec = "openai:o3-mini"
        params = with_effort_model_params(spec, {}, "medium")
        assert params["reasoning_effort"] == "medium"
        # Crucial fix: must NOT inject nested 'reasoning' dict to prevent Responses API collision
        assert "reasoning" not in params

    def test_all_supported_providers_with_effort(self):
        """Verify flat reasoning_effort is consistently set across all provider families."""
        providers_and_models = [
            ("openai:gpt-5.5-pro", "high"),
            ("azure_openai:gpt-5.5-pro", "medium"),
            ("anthropic:claude-3-7-sonnet", "high"),
            ("google_genai:gemini-2.5-flash", "low"),
            ("google_vertexai:gemini-3.8-flash", "high"),
            ("groq:deepseek-r1-distill-llama-70b", "medium"),
            ("deepseek:deepseek-reasoner", "medium"),
            ("fireworks:accounts/fireworks/models/deepseek-v4-pro", "high"),
            ("xai:grok-4.5", "low"),
            ("openrouter:moonshotai/kimi-k3", "medium"),
        ]
        for spec, effort in providers_and_models:
            params = with_effort_model_params(spec, {"temperature": 0.5}, effort)
            assert params["reasoning_effort"] == effort, f"Failed for {spec}"
            assert params["temperature"] == 0.5, f"Preserved siblings for {spec}"
            assert "reasoning" not in params, f"No nested reasoning for {spec}"

    def test_remove_nested_key_helper(self):
        # Cleans key while preserving siblings
        data = {"reasoning": {"effort": "high", "type": "enabled"}}
        _remove_nested_key(data, "reasoning", "effort")
        assert data == {"reasoning": {"type": "enabled"}}

        # Removes entire container when empty
        data2 = {"reasoning": {"effort": "high"}}
        _remove_nested_key(data2, "reasoning", "effort")
        assert data2 == {}

        # Safe when container is not a Mapping
        data3 = {"reasoning": "invalid"}
        _remove_nested_key(data3, "reasoning", "effort")
        assert data3 == {"reasoning": "invalid"}

    def test_without_effort_model_params_across_providers(self):
        # Google
        spec_g = "google_genai:gemini-3.7-flash"
        params_g = {
            "reasoning_effort": "high",
            "thinking_level": "high",
            "thinking_config": {"thinking_level": "high", "other_setting": True},
            "temperature": 0.7,
        }
        cleaned_g = without_effort_model_params(spec_g, params_g)
        assert cleaned_g is not None
        assert "reasoning_effort" not in cleaned_g
        assert "thinking_level" not in cleaned_g
        assert cleaned_g["thinking_config"] == {"other_setting": True}
        assert cleaned_g["temperature"] == 0.7

        # OpenAI
        spec_o = "openai:gpt-5.5-pro"
        params_o = {
            "reasoning_effort": "medium",
            "reasoning": {"effort": "medium", "max_tokens": 4096},
            "stream": True,
        }
        cleaned_o = without_effort_model_params(spec_o, params_o)
        assert cleaned_o is not None
        assert "reasoning_effort" not in cleaned_o
        assert cleaned_o["reasoning"] == {"max_tokens": 4096}
        assert cleaned_o["stream"] is True

        # Anthropic
        spec_a = "anthropic:claude-3-7-sonnet"
        params_a = {
            "reasoning_effort": "high",
            "effort": "high",
            "output_config": {"effort": "high"},
            "cache_control": True,
        }
        cleaned_a = without_effort_model_params(spec_a, params_a)
        assert cleaned_a is not None
        assert "reasoning_effort" not in cleaned_a
        assert "effort" not in cleaned_a
        assert "output_config" not in cleaned_a
        assert cleaned_a["cache_control"] is True

        # Fireworks & xAI
        cleaned_fw = without_effort_model_params(
            "fireworks:model",
            {"model_kwargs": {"reasoning_effort": "high"}},
        )
        assert cleaned_fw is None

        cleaned_xai = without_effort_model_params(
            "xai:model",
            {"extra_body": {"reasoning_effort": "high"}},
        )
        assert cleaned_xai is None

    def test_current_effort_from_model_params(self):
        assert current_effort_from_model_params("openai:o3-mini", {"reasoning": {"effort": "high"}}) == "high"
        assert current_effort_from_model_params("openai:o3-mini", {"reasoning_effort": "low"}) == "low"
        assert current_effort_from_model_params("anthropic:claude-3-7-sonnet", {"effort": "medium"}) == "medium"
        assert current_effort_from_model_params("anthropic:claude-3-7-sonnet", {"output_config": {"effort": "high"}}) == "high"
        assert current_effort_from_model_params("google_genai:gemini-2.5-pro", {"thinking_level": "low"}) == "low"
        assert current_effort_from_model_params("google_genai:gemini-2.5-pro", {"thinking_config": {"thinking_level": "medium"}}) == "medium"

    def test_has_explicit_effort_model_params(self):
        assert has_explicit_effort_model_params("openai:o3-mini", {"reasoning": {"effort": "high"}}) is True
        assert has_explicit_effort_model_params("openai:o3-mini", {"temperature": 0.5}) is False
        assert has_explicit_effort_model_params("anthropic:claude-3-7-sonnet", {"output_config": {"effort": "low"}}) is True
        assert has_explicit_effort_model_params("google_genai:gemini-2.5-flash", {"thinking_level": "low"}) is True

    def test_model_specific_effort_levels(self):
        # Gemini 3.1 Pro only supports low and high
        gemini_efforts = supported_efforts_for_model("google_genai:gemini-3.1-pro")
        assert gemini_efforts == ("low", "high")
        assert default_effort_for_model("google_genai:gemini-3.1-pro") == "low"
        assert is_effort_supported_for_model("google_genai:gemini-3.1-pro", "low") is True
        assert is_effort_supported_for_model("google_genai:gemini-3.1-pro", "high") is True
        assert is_effort_supported_for_model("google_genai:gemini-3.1-pro", "medium") is False
        assert is_effort_supported_for_model("google_genai:gemini-3.1-pro", "off") is True

        # Non-reasoning model returns empty tuple and None default
        assert supported_efforts_for_model("openai:gpt-4o") == ()
        assert default_effort_for_model("openai:gpt-4o") is None

        # Claude Opus 4.8 has extended reasoning levels up to max
        opus_efforts = supported_efforts_for_model("anthropic:claude-opus-4-8")
        assert opus_efforts == ("low", "medium", "high", "xhigh", "max")
        assert default_effort_for_model("anthropic:claude-opus-4-8") == "high"
