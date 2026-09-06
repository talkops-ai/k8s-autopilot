"""Unit tests for dynamic reasoning effort — provider translation, profiles, and round-trip verification."""

from __future__ import annotations

import pytest


class TestModelProfiles:
    def test_exact_match(self):
        from k8s_autopilot.model.reasoning import get_model_profile

        profile = get_model_profile("google_genai:gemini-3.7-flash")
        assert profile is not None
        assert profile["reasoning_output"] is True

    def test_prefix_match(self):
        from k8s_autopilot.model.reasoning import get_model_profile

        profile = get_model_profile("google_genai:gemini-3.7-flash-preview-0325")
        assert profile is not None

    def test_unknown_model(self):
        from k8s_autopilot.model.reasoning import get_model_profile

        assert get_model_profile("unknown:model") is None


class TestSupportedEfforts:
    def test_gemini_pro_levels(self):
        from k8s_autopilot.model.reasoning import supported_efforts_for_model

        levels = supported_efforts_for_model("google_genai:gemini-3.1-pro")
        assert levels == ("low", "high")

    def test_gemini_flash_levels(self):
        from k8s_autopilot.model.reasoning import supported_efforts_for_model

        levels = supported_efforts_for_model("google_genai:gemini-3.7-flash")
        assert levels == ("low", "medium", "high")

    def test_unknown_model_defaults(self):
        from k8s_autopilot.model.reasoning import supported_efforts_for_model

        assert supported_efforts_for_model("unknown:model") == ("low", "medium", "high")


class TestDefaultEffort:
    def test_gemini_pro(self):
        from k8s_autopilot.model.reasoning import default_effort_for_model

        assert default_effort_for_model("google_genai:gemini-3.1-pro") == "low"

    def test_gemini_flash(self):
        from k8s_autopilot.model.reasoning import default_effort_for_model

        assert default_effort_for_model("google_genai:gemini-3.7-flash") == "high"

    def test_unknown_model(self):
        from k8s_autopilot.model.reasoning import default_effort_for_model

        assert default_effort_for_model("unknown:model") is None


class TestEffortSupported:
    def test_supported(self):
        from k8s_autopilot.model.reasoning import is_effort_supported_for_model

        assert is_effort_supported_for_model("google_genai:gemini-3.1-pro", "high")
        assert is_effort_supported_for_model("google_genai:gemini-3.1-pro", "low")

    def test_not_supported(self):
        from k8s_autopilot.model.reasoning import is_effort_supported_for_model

        assert not is_effort_supported_for_model("google_genai:gemini-3.1-pro", "medium")
        assert not is_effort_supported_for_model("google_genai:gemini-3.1-pro", "max")


class TestGeminiEffortParams:
    def test_high_effort(self):
        from k8s_autopilot.model.reasoning import with_effort_model_params

        result = with_effort_model_params("google_genai:gemini-3.7-flash", None, "high")
        assert result["reasoning_effort"] == "high"
        assert result["include_thoughts"] is True
        assert result["thinking_level"] == "high"
        assert result["thinking_budget"] == 8192

    def test_low_effort(self):
        from k8s_autopilot.model.reasoning import with_effort_model_params

        result = with_effort_model_params("google_genai:gemini-3.7-flash", None, "low")
        assert result["thinking_budget"] == 1024


class TestAnthropicEffortParams:
    def test_medium_effort(self):
        from k8s_autopilot.model.reasoning import with_effort_model_params

        result = with_effort_model_params("anthropic:claude-sonnet-4", None, "medium")
        assert result["reasoning_effort"] == "medium"
        assert result["thinking"]["type"] == "enabled"
        assert result["thinking"]["budget_tokens"] == 4096


class TestOpenAIEffortParams:
    def test_low_effort(self):
        from k8s_autopilot.model.reasoning import with_effort_model_params

        result = with_effort_model_params("openai:o3", None, "low")
        assert result["reasoning_effort"] == "low"
        assert result["reasoning"]["effort"] == "low"


class TestEffortParamCleanup:
    def test_removes_native_params(self):
        from k8s_autopilot.model.reasoning import without_effort_model_params

        params = {
            "reasoning_effort": "high",
            "thinking_level": "HIGH",
            "some_other": "value",
        }
        cleaned = without_effort_model_params("google_genai:gemini-3.7-flash", params)
        assert "reasoning_effort" not in cleaned
        assert "thinking_level" not in cleaned
        assert cleaned["some_other"] == "value"

    def test_none_input(self):
        from k8s_autopilot.model.reasoning import without_effort_model_params

        assert without_effort_model_params("openai:o3", None) is None

    def test_openai_nested_cleanup(self):
        from k8s_autopilot.model.reasoning import without_effort_model_params

        params = {"reasoning": {"effort": "high"}, "temperature": 0.5}
        cleaned = without_effort_model_params("openai:o3", params)
        assert "reasoning" not in cleaned
        assert cleaned["temperature"] == 0.5


class TestEffortDetection:
    def test_openai_detected(self):
        from k8s_autopilot.model.reasoning import has_explicit_effort_model_params

        assert has_explicit_effort_model_params("openai:o3", {"reasoning": {"effort": "high"}})

    def test_empty_not_detected(self):
        from k8s_autopilot.model.reasoning import has_explicit_effort_model_params

        assert not has_explicit_effort_model_params("openai:o3", {})

    def test_none_not_detected(self):
        from k8s_autopilot.model.reasoning import has_explicit_effort_model_params

        assert not has_explicit_effort_model_params(None, None)


class TestCurrentEffortReading:
    def test_openai(self):
        from k8s_autopilot.model.reasoning import current_effort_from_model_params

        assert current_effort_from_model_params("openai:o3", {"reasoning": {"effort": "medium"}}) == "medium"

    def test_gemini(self):
        from k8s_autopilot.model.reasoning import current_effort_from_model_params

        assert current_effort_from_model_params("google_genai:gemini-3.7-flash", {"thinking_level": "HIGH"}) == "HIGH"

    def test_anthropic(self):
        from k8s_autopilot.model.reasoning import current_effort_from_model_params

        assert current_effort_from_model_params("anthropic:claude-sonnet-4", {"effort": "low"}) == "low"

    def test_canonical_fallback(self):
        from k8s_autopilot.model.reasoning import current_effort_from_model_params

        assert current_effort_from_model_params("unknown:model", {"reasoning_effort": "high"}) == "high"


class TestEffortRoundTrip:
    @pytest.mark.parametrize("spec", [
        "google_genai:gemini-3.7-flash",
        "anthropic:claude-sonnet-4",
        "openai:o3",
    ])
    def test_set_read_remove(self, spec):
        from k8s_autopilot.model.reasoning import (
            with_effort_model_params,
            current_effort_from_model_params,
            without_effort_model_params,
            has_explicit_effort_model_params,
        )

        params = with_effort_model_params(spec, None, "high")
        assert current_effort_from_model_params(spec, params) is not None
        assert has_explicit_effort_model_params(spec, params)

        cleaned = without_effort_model_params(spec, params)
        assert not has_explicit_effort_model_params(spec, cleaned)


class TestConfigurableModelMiddleware:
    def test_dynamic_model_settings_openai(self):
        from langchain.agents.middleware.types import ModelRequest, ModelResponse
        from langchain_core.messages import AIMessage
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware

        middleware = ConfigurableModelMiddleware(persist_model_state=False)

        fake_model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])

        class FakeRuntime:
            context = {
                "model": "openai:o3-mini",
                "model_params": {"reasoning": {"effort": "high"}},
            }

        req = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings={}, runtime=FakeRuntime())

        captured_req = []
        def fake_handler(r):
            captured_req.append(r)
            return ModelResponse(result=[AIMessage(content="ok")])

        resp = middleware.wrap_model_call(req, fake_handler)
        assert len(captured_req) == 1
        assert captured_req[0].model_settings["reasoning"] == {"effort": "high"}

    def test_dynamic_model_settings_anthropic(self):
        from langchain.agents.middleware.types import ModelRequest, ModelResponse
        from langchain_core.messages import AIMessage
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware

        middleware = ConfigurableModelMiddleware(persist_model_state=False)

        fake_model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])

        class FakeRuntime:
            context = {
                "model": "anthropic:claude-3-7-sonnet-latest",
                "model_params": {"thinking": {"type": "enabled", "budget_tokens": 8192}},
            }

        req = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings={}, runtime=FakeRuntime())

        captured_req = []
        def fake_handler(r):
            captured_req.append(r)
            return ModelResponse(result=[AIMessage(content="ok")])

        resp = middleware.wrap_model_call(req, fake_handler)
        assert len(captured_req) == 1
        assert captured_req[0].model_settings["thinking"] == {"type": "enabled", "budget_tokens": 8192}

    def test_dynamic_model_settings_google(self):
        from langchain.agents.middleware.types import ModelRequest, ModelResponse
        from langchain_core.messages import AIMessage
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware

        middleware = ConfigurableModelMiddleware(persist_model_state=False)

        fake_model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])

        class FakeRuntime:
            context = {
                "model": "google_genai:gemini-3.6-flash",
                "model_params": {"thinking_level": "HIGH", "include_thoughts": True},
            }

        req = ModelRequest(model=fake_model, messages=[], system_prompt=None, model_settings={}, runtime=FakeRuntime())

        captured_req = []
        def fake_handler(r):
            captured_req.append(r)
            return ModelResponse(result=[AIMessage(content="ok")])

        resp = middleware.wrap_model_call(req, fake_handler)
        assert len(captured_req) == 1
        assert captured_req[0].model_settings["thinking_level"] == "HIGH"
        assert captured_req[0].model_settings["include_thoughts"] is True

