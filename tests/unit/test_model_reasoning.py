"""Unit tests for reasoning effort and provider-native thinking level transformation."""

from __future__ import annotations

import pytest

from k8s_autopilot.model.reasoning import (
    default_effort_for_model,
    is_effort_supported_for_model,
    supported_efforts_for_model,
    with_effort_model_params,
    without_effort_model_params,
)


class TestModelReasoning:
    """Test thinking level and effort injection."""

    def test_supported_efforts(self):
        efforts = supported_efforts_for_model("google_genai:gemini-3.7-flash")
        assert "high" in efforts
        assert "medium" in efforts
        assert "low" in efforts

    def test_is_effort_supported(self):
        assert is_effort_supported_for_model("google_genai:gemini-3.7-flash", "high") is True
        assert is_effort_supported_for_model("anthropic:claude-3-7-sonnet", "low") is True

    def test_google_genai_thinking_params(self):
        spec = "google_genai:gemini-3.7-flash"
        params = with_effort_model_params(spec, {}, "high")
        assert params["include_thoughts"] is True
        assert params["thinking_level"] == "high"
        assert params["thinking_budget"] == 8192
        assert params["reasoning_effort"] == "high"

        max_params = with_effort_model_params(spec, {}, "max")
        assert max_params["thinking_budget"] == 16384

    def test_anthropic_thinking_params(self):
        spec = "anthropic:claude-3-7-sonnet"
        params = with_effort_model_params(spec, {}, "high")
        assert params["thinking"]["type"] == "enabled"
        assert params["thinking"]["budget_tokens"] == 8192

        low_params = with_effort_model_params(spec, {}, "low")
        assert low_params["thinking"]["budget_tokens"] == 1024

    def test_openai_reasoning_params(self):
        spec = "openai:o3-mini"
        params = with_effort_model_params(spec, {}, "medium")
        assert params["reasoning"]["effort"] == "medium"

    def test_without_effort_model_params(self):
        spec = "google_genai:gemini-3.7-flash"
        params = {"include_thoughts": True, "thinking_level": "HIGH", "temperature": 0.7}
        cleaned = without_effort_model_params(spec, params)
        assert cleaned is not None
        assert "thinking_level" not in cleaned
        assert cleaned["temperature"] == 0.7
