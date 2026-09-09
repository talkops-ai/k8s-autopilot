"""Comprehensive test suite for multi-provider reasoning effort and payload safety.

Verifies that model creation, reasoning effort transformation, payload construction,
and dynamic middleware switching work seamlessly across all supported providers
without raising unexpected keyword arguments (e.g. AsyncResponses.create(**payload)).
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware
from k8s_autopilot.model.config import MODEL_PROFILES, ModelSpec
from k8s_autopilot.model.factory import (
    _compose_openai_reasoning_effort,
    create_model,
)
from k8s_autopilot.model.reasoning import (
    current_effort_from_model_params,
    has_explicit_effort_model_params,
    is_effort_supported_for_model,
    supported_efforts_for_model,
    with_effort_model_params,
    without_effort_model_params,
)


@pytest.fixture(autouse=True)
def mock_all_provider_keys(monkeypatch: pytest.MonkeyPatch):
    """Provide mock credentials for all provider families during testing."""
    env_keys = {
        "OPENAI_API_KEY": "sk-mock-openai-key",
        "AZURE_OPENAI_API_KEY": "mock-azure-key",
        "AZURE_OPENAI_ENDPOINT": "https://mock.openai.azure.com",
        "ANTHROPIC_API_KEY": "sk-ant-mock-key",
        "GOOGLE_API_KEY": "AIzaSyMockKey",
        "GEMINI_API_KEY": "AIzaSyMockKey",
        "GROQ_API_KEY": "gsk_mock_key",
        "DEEPSEEK_API_KEY": "dsk_mock_key",
        "MISTRAL_API_KEY": "mistral_mock_key",
        "FIREWORKS_API_KEY": "fw_mock_key",
        "TOGETHER_API_KEY": "together_mock_key",
        "COHERE_API_KEY": "cohere_mock_key",
        "XAI_API_KEY": "xai_mock_key",
        "OPENROUTER_API_KEY": "sk-or-mock-key",
    }
    for k, v in env_keys.items():
        monkeypatch.setenv(k, v)


class TestAllProvidersReasoningSafety:
    """Ensure no provider fails with unexpected keyword arguments or key collisions."""

    @pytest.mark.parametrize(
        ("spec", "effort"),
        [
            ("openai:gpt-5.5-pro", "high"),
            ("openai:gpt-5.5", "medium"),
            ("openai:gpt-5.4-mini", "low"),
            ("openai:o3-mini", "medium"),
            ("azure_openai:gpt-5.5-pro", "high"),
            ("anthropic:claude-3-7-sonnet", "high"),
            ("anthropic:claude-sonnet-5", "max"),
            ("google_genai:gemini-3.7-flash", "high"),
            ("google_genai:gemini-2.5-flash", "medium"),
            ("google_vertexai:gemini-3.8-flash", "low"),
            ("fireworks:accounts/fireworks/models/deepseek-v4-pro", "high"),
            ("fireworks:accounts/fireworks/models/glm-5p2", "max"),
            ("xai:grok-4.5", "low"),
            ("openrouter:moonshotai/kimi-k3", "medium"),
            ("openrouter:anthropic/claude-sonnet-5", "high"),
        ],
    )
    def test_reasoning_models_support_and_params(self, spec: str, effort: str):
        """Verify supported reasoning models correctly accept canonical reasoning_effort."""
        # Supported check
        assert is_effort_supported_for_model(spec, effort) is True

        # with_effort_model_params must set appropriate provider params
        params = with_effort_model_params(spec, {}, effort)
        assert params["reasoning_effort"] == effort
        if spec.startswith(("openai:", "azure_openai:")):
            assert "reasoning" not in params
        elif spec.startswith("google_"):
            assert params.get("include_thoughts") is True
            assert params.get("thinking_level") == effort
        elif spec.startswith("anthropic:"):
            assert isinstance(params.get("thinking"), dict)
            assert params["thinking"].get("type") == "enabled"

        # current_effort_from_model_params must read it back
        assert current_effort_from_model_params(spec, params) == effort
        assert has_explicit_effort_model_params(spec, params) is True

        # without_effort_model_params must clean it completely
        cleaned = without_effort_model_params(spec, params)
        assert cleaned is None or "reasoning_effort" not in cleaned

    @pytest.mark.parametrize(
        "non_reasoning_spec",
        [
            "openai:gpt-4o",
            "openai:gpt-4o-mini",
            "groq:llama-3.3-70b-versatile",
            "groq:deepseek-r1-distill-llama-70b",
            "deepseek:deepseek-chat",
            "deepseek:deepseek-reasoner",
            "mistralai:mistral-large-latest",
            "together:meta-llama/Llama-3-70b-chat-hf",
            "cohere:command-r-plus",
            "xai:grok-2-latest",
        ],
    )
    def test_non_reasoning_models_do_not_force_effort(self, non_reasoning_spec: str):
        """Non-reasoning models must report empty supported efforts and reject arbitrary effort levels."""
        assert supported_efforts_for_model(non_reasoning_spec) == ()
        assert is_effort_supported_for_model(non_reasoning_spec, "high") is False
        assert is_effort_supported_for_model(non_reasoning_spec, "medium") is False
        assert is_effort_supported_for_model(non_reasoning_spec, "low") is False
        # 'off' and 'none' are always accepted
        assert is_effort_supported_for_model(non_reasoning_spec, "off") is True
        assert is_effort_supported_for_model(non_reasoning_spec, "none") is True

    def test_openai_responses_api_payload_construction(self):
        """Directly verify ChatOpenAI's internal Responses API payload generator."""
        from langchain_openai import ChatOpenAI

        # Flat reasoning_effort: maps to reasoning={"effort": "medium"}, reasoning_effort POPPED
        m = ChatOpenAI(model="gpt-5.5-pro", reasoning_effort="medium", use_responses_api=True)
        payload = m._get_request_payload([{"role": "user", "content": "test query"}])
        assert "reasoning_effort" not in payload
        assert payload.get("reasoning") == {"effort": "medium"}

    def test_openai_compositions_prevent_collision(self):
        """Verify _compose_openai_reasoning_effort handles both flat and existing nested dicts."""
        # 1. Existing reasoning mapping (e.g. from prior config or profile)
        existing = {
            "reasoning_effort": "high",
            "reasoning": {"type": "enabled", "effort": "low"},
            "temperature": 0.0,
        }
        composed = _compose_openai_reasoning_effort("openai", existing, "high")
        assert "reasoning_effort" not in composed
        assert composed["reasoning"] == {"type": "enabled", "effort": "high"}

        # 2. Azure OpenAI
        azure_composed = _compose_openai_reasoning_effort("azure_openai", existing, "medium")
        assert "reasoning_effort" not in azure_composed
        assert azure_composed["reasoning"] == {"type": "enabled", "effort": "medium"}

    def test_middleware_switching_between_all_provider_types(self):
        """Simulate dynamic cross-provider switching in ConfigurableModelMiddleware."""
        middleware = ConfigurableModelMiddleware(persist_model_state=False)
        fake_model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])

        # Step 1: Turn 1 on Anthropic with Anthropic-only settings
        anthropic_settings = {
            "cache_control": True,
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "reasoning_effort": "medium",
        }
        class Runtime1:
            context = {"model": "anthropic:claude-3-7-sonnet", "model_params": {"reasoning_effort": "medium"}}
        req1 = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings=anthropic_settings, runtime=Runtime1())

        captured1 = []
        middleware.wrap_model_call(req1, lambda r: captured1.append(r) or ModelResponse(result=[AIMessage(content="ok")]))
        # Anthropic gets reasoning_effort
        assert captured1[0].model_settings["reasoning_effort"] == "medium"

        # Step 2: Turn 2 switches from Anthropic to OpenAI (gpt-5.5-pro)
        class Runtime2:
            context = {"model": "openai:gpt-5.5-pro", "model_params": {"reasoning_effort": "high"}}
        # Inbound request has Anthropic settings carried from prior state
        req2 = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings=anthropic_settings, runtime=Runtime2())

        captured2 = []
        middleware.wrap_model_call(req2, lambda r: captured2.append(r) or ModelResponse(result=[AIMessage(content="ok")]))
        settings2 = captured2[0].model_settings

        # Anthropic keys MUST be stripped
        assert "cache_control" not in settings2
        assert "thinking" not in settings2
        # OpenAI canonical reasoning_effort is active
        assert settings2["reasoning_effort"] == "high"
        assert "reasoning" not in settings2

        # Step 3: Turn 3 switches from OpenAI to Google GenAI
        openai_settings = {"reasoning": {"effort": "high"}, "reasoning_effort": "high"}
        class Runtime3:
            context = {"model": "google_genai:gemini-2.5-flash", "model_params": {"reasoning_effort": "low"}}
        req3 = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings=openai_settings, runtime=Runtime3())

        captured3 = []
        middleware.wrap_model_call(req3, lambda r: captured3.append(r) or ModelResponse(result=[AIMessage(content="ok")]))
        settings3 = captured3[0].model_settings

        # OpenAI keys MUST be stripped
        assert "reasoning" not in settings3
        # Google canonical reasoning_effort is active
        assert settings3["reasoning_effort"] == "low"

        # Step 4: Turn 4 switches to non-reasoning Groq model
        class Runtime4:
            context = {"model": "groq:llama-3.3-70b-versatile", "model_params": {}}
        req4 = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings=settings3, runtime=Runtime4())

        captured4 = []
        middleware.wrap_model_call(req4, lambda r: captured4.append(r) or ModelResponse(result=[AIMessage(content="ok")]))
        settings4 = captured4[0].model_settings
        # Google thinking keys MUST be stripped, no reasoning settings leak
        assert "thinking_level" not in settings4
        assert "thinking_budget" not in settings4
        assert "reasoning" not in settings4
