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
    """Tests for reasoning effort mapping."""

    def test_google_thinking_tokens(self) -> None:
        from k8s_autopilot.model.reasoning import with_effort_model_params

        params = with_effort_model_params("google_genai:gemini-3.5-flash", {}, "high")
        assert params["reasoning_effort"] == "high"
        assert params["thinking_budget"] == 8192

    def test_openai_effort(self) -> None:
        from k8s_autopilot.model.reasoning import with_effort_model_params

        params = with_effort_model_params("openai:o3-mini", {}, "low")
        assert params["reasoning_effort"] == "low"
        assert params["reasoning"]["effort"] == "low"

    def test_unknown_provider_passthrough(self) -> None:
        from k8s_autopilot.model.reasoning import with_effort_model_params

        params = with_effort_model_params("unknown:model", {"key": "val"}, "high")
        assert params["key"] == "val"
        assert params["reasoning_effort"] == "high"


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

