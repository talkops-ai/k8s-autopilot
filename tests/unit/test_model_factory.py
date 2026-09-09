"""Unit tests for Model Factory (Phase 15)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDetectProvider:
    """Tests for provider auto-detection from model names."""

    def test_openai_models(self) -> None:
        from k8s_autopilot.model.factory import detect_provider

        assert detect_provider("gpt-4o") == "openai"
        assert detect_provider("gpt-4o-mini") == "openai"
        assert detect_provider("o3-mini") == "openai"
        assert detect_provider("o4-mini") == "openai"

    def test_anthropic_models(self) -> None:
        from k8s_autopilot.model.factory import detect_provider

        assert detect_provider("claude-3-5-sonnet-latest") == "anthropic"
        assert detect_provider("claude-4-opus") == "anthropic"

    def test_google_models(self) -> None:
        from k8s_autopilot.model.factory import detect_provider

        assert detect_provider("gemini-3.5-flash") == "google_genai"
        assert detect_provider("gemini-2.5-pro") == "google_genai"

    def test_deepseek_models(self) -> None:
        from k8s_autopilot.model.factory import detect_provider

        assert detect_provider("deepseek-chat") == "deepseek"

    def test_unknown_model(self) -> None:
        from k8s_autopilot.model.factory import detect_provider

        assert detect_provider("totally-unknown-model") is None


class TestNormalizeModelSpec:
    """Tests for model spec normalization."""

    def test_already_normalized(self) -> None:
        from k8s_autopilot.model.factory import normalize_model_spec

        assert normalize_model_spec("openai:gpt-4o") == "openai:gpt-4o"

    def test_bare_model_name(self) -> None:
        from k8s_autopilot.model.factory import normalize_model_spec

        assert normalize_model_spec("gpt-4o") == "openai:gpt-4o"
        assert normalize_model_spec("claude-3-5-sonnet-latest") == "anthropic:claude-3-5-sonnet-latest"
        assert normalize_model_spec("gemini-3.5-flash") == "google_genai:gemini-3.5-flash"

    def test_unknown_remains_bare(self) -> None:
        from k8s_autopilot.model.factory import normalize_model_spec

        assert normalize_model_spec("mystery-model") == "mystery-model"

    def test_empty_passthrough(self) -> None:
        from k8s_autopilot.model.factory import normalize_model_spec

        assert normalize_model_spec("") == ""


class TestModelSpec:
    """Tests for ModelSpec dataclass."""

    def test_try_parse_valid(self) -> None:
        from k8s_autopilot.model.config import ModelSpec

        spec = ModelSpec.try_parse("openai:gpt-4o")
        assert spec is not None
        assert spec.provider == "openai"
        assert spec.model == "gpt-4o"

    def test_try_parse_no_colon(self) -> None:
        from k8s_autopilot.model.config import ModelSpec

        assert ModelSpec.try_parse("gpt-4o") is None


class TestModelConfig:
    """Tests for ModelConfig."""

    def test_credential_env_vars(self) -> None:
        from k8s_autopilot.model.config import get_credential_env_var

        assert get_credential_env_var("openai") == "OPENAI_API_KEY"
        assert get_credential_env_var("anthropic") == "ANTHROPIC_API_KEY"
        assert get_credential_env_var("google_genai") == "GOOGLE_API_KEY"
        assert get_credential_env_var("unknown") is None

    def test_has_provider_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from k8s_autopilot.config.settings import get_settings
        from k8s_autopilot.model.config import has_provider_credentials

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert has_provider_credentials("openai") is True

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("K8S_AUTOPILOT_OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(get_settings(), "openai_api_key", None)
        assert has_provider_credentials("openai") is False

    def test_implicit_auth_providers(self) -> None:
        from k8s_autopilot.model.config import has_provider_credentials

        assert has_provider_credentials("ollama") is True
        assert has_provider_credentials("bedrock") is True


class TestModelResult:
    """Tests for ModelResult dataclass."""

    def test_model_result_creation(self) -> None:
        from k8s_autopilot.model.factory import ModelResult

        result = ModelResult(
            model=MagicMock(),
            model_name="gpt-4o",
            provider="openai",
            context_limit=128_000,
        )
        assert result.model_name == "gpt-4o"
        assert result.provider == "openai"
        assert result.context_limit == 128_000
        assert result.unsupported_modalities == frozenset()


class TestReasoningEffort:
    """Tests for reasoning effort mapping across providers."""

    def test_google_effort(self) -> None:
        from k8s_autopilot.model.reasoning import with_effort_model_params

        params = with_effort_model_params("google_genai:gemini-3.5-flash", {}, "high")
        assert params["reasoning_effort"] == "high"
        assert "thinking_budget" not in params

    def test_openai_effort(self) -> None:
        from k8s_autopilot.model.reasoning import with_effort_model_params

        params = with_effort_model_params("openai:o3-mini", {}, "low")
        assert params["reasoning_effort"] == "low"
        assert "reasoning" not in params

    def test_unknown_provider_passthrough(self) -> None:
        from k8s_autopilot.model.reasoning import with_effort_model_params

        params = with_effort_model_params("unknown:model", {"key": "val"}, "high")
        assert params["key"] == "val"
        assert params["reasoning_effort"] == "high"

    def test_compose_openai_reasoning_effort(self) -> None:
        from k8s_autopilot.model.factory import _compose_openai_reasoning_effort

        # 1. Non-OpenAI providers are untouched
        anthropic_kwargs = {"reasoning_effort": "high", "reasoning": {"effort": "low"}}
        assert _compose_openai_reasoning_effort("anthropic", anthropic_kwargs, "high") == anthropic_kwargs

        # 2. OpenAI with no existing reasoning dict keeps flat reasoning_effort
        flat_kwargs = {"reasoning_effort": "medium", "temperature": 0.0}
        assert _compose_openai_reasoning_effort("openai", flat_kwargs, "medium") == flat_kwargs

        # 3. OpenAI with existing reasoning dict composes effort and removes reasoning_effort
        nested_kwargs = {
            "reasoning_effort": "high",
            "reasoning": {"type": "enabled", "effort": "low"},
            "temperature": 0.0,
        }
        composed = _compose_openai_reasoning_effort("openai", nested_kwargs, "high")
        assert "reasoning_effort" not in composed
        assert composed["reasoning"] == {"type": "enabled", "effort": "high"}
        assert composed["temperature"] == 0.0

        # 4. Azure OpenAI is handled identically
        composed_azure = _compose_openai_reasoning_effort("azure_openai", nested_kwargs, "medium")
        assert "reasoning_effort" not in composed_azure
        assert composed_azure["reasoning"]["effort"] == "medium"

    def test_openai_responses_api_payload_no_reasoning_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify ChatOpenAI with use_responses_api does NOT have reasoning_effort in API payload."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-mock-key")
        from langchain_openai import ChatOpenAI

        # Case 1: Model constructed with flat reasoning_effort
        m1 = ChatOpenAI(model="gpt-4o", reasoning_effort="medium", use_responses_api=True)
        payload1 = m1._get_request_payload([{"role": "user", "content": "hello"}])
        assert "reasoning_effort" not in payload1
        assert payload1.get("reasoning") == {"effort": "medium"}

        # Case 2: Model constructed with composed reasoning dict
        m2 = ChatOpenAI(model="gpt-4o", reasoning={"effort": "high"}, use_responses_api=True)
        payload2 = m2._get_request_payload([{"role": "user", "content": "hello"}])
        assert "reasoning_effort" not in payload2
        assert payload2.get("reasoning") == {"effort": "high"}

    def test_anthropic_payload_output_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify ChatAnthropic maps reasoning_effort to output_config without reasoning_effort in payload."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from langchain_anthropic import ChatAnthropic

        m = ChatAnthropic(model="claude-3-7-sonnet-20250219", reasoning_effort="high")
        payload = m._get_request_payload([{"role": "user", "content": "hello"}])
        assert "reasoning_effort" not in payload
        assert "output_config" in payload
        assert payload["output_config"] == {"effort": "high"}

    def test_multi_provider_create_model_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify create_model applies profile and reasoning kwargs cleanly across providers."""
        from k8s_autopilot.model.factory import create_model

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyTest")
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

        # OpenAI
        res_o = create_model("openai:gpt-5.5-pro", extra_kwargs={"reasoning_effort": "high"})
        assert getattr(res_o.model, "use_responses_api", False) is True
        assert getattr(res_o.model, "reasoning_effort", None) == "high"

        # Anthropic
        res_a = create_model("anthropic:claude-3-7-sonnet", extra_kwargs={"reasoning_effort": "medium"})
        assert getattr(res_a.model, "reasoning_effort", None) == "medium"

        # Google GenAI
        res_g = create_model("google_genai:gemini-2.5-flash", extra_kwargs={"reasoning_effort": "low"})
        assert getattr(res_g.model, "reasoning_effort", None) == "low"

        # Groq (non-reasoning model: extra reasoning_effort ignored/not crashing)
        res_groq = create_model("groq:llama-3.3-70b-versatile")
        assert res_groq.provider == "groq"


class TestStrictModelSelection:
    """Tests ensuring no silent fallbacks are used when selecting models."""

    def test_missing_credentials_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from k8s_autopilot.config import paths
        from k8s_autopilot.exceptions import MissingCredentialsError
        from k8s_autopilot.model.factory import create_model
        from k8s_autopilot.config.settings import get_settings

        monkeypatch.setattr(paths, "GLOBAL_ENV_PATH", tmp_path / ".empty_env")
        monkeypatch.setattr("k8s_autopilot.config.settings._load_dotenv", lambda **kw: None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("K8S_AUTOPILOT_OPENAI_API_KEY", raising=False)

        settings = get_settings()
        monkeypatch.setattr(settings, "openai_api_key", None)

        with pytest.raises(MissingCredentialsError) as exc:
            create_model("openai:gpt-4o")
        assert "openai" in str(exc.value)

    def test_no_credentials_configured_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from k8s_autopilot.config import paths
        from k8s_autopilot.exceptions import NoCredentialsConfiguredError
        from k8s_autopilot.model.factory import _get_default_model_spec
        from k8s_autopilot.config.settings import get_settings

        monkeypatch.setattr(paths, "GLOBAL_ENV_PATH", tmp_path / ".empty_env")
        monkeypatch.setattr("k8s_autopilot.config.settings._load_dotenv", lambda **kw: None)

        for key in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
            "GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
            "OPENROUTER_API_KEY", "MODEL", "MODEL_NAME"
        ):
            monkeypatch.delenv(key, raising=False)
            monkeypatch.delenv(f"K8S_AUTOPILOT_{key}", raising=False)

        settings = get_settings()
        monkeypatch.setattr(settings, "model", None)
        monkeypatch.setattr(settings, "model_name", None)
        monkeypatch.setattr(settings, "openai_api_key", None)
        monkeypatch.setattr(settings, "anthropic_api_key", None)
        monkeypatch.setattr(settings, "google_api_key", None)
        monkeypatch.setattr(settings, "groq_api_key", None)
        monkeypatch.setattr(settings, "deepseek_api_key", None)
        monkeypatch.setattr(settings, "openrouter_api_key", None)

        with pytest.raises(NoCredentialsConfiguredError):
            _get_default_model_spec()

