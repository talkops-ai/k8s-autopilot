"""Unit tests for LangSmith configuration, URL resolution, and environment synchronization."""

import os
from unittest.mock import MagicMock, patch

import pytest

from k8s_autopilot.config.langsmith import (
    LangSmithApiError,
    LangSmithImportError,
    LangSmithLookupTimeoutError,
    LangSmithProjectNotFoundError,
    _assemble_langsmith_thread_url,
    apply_tracing_settings,
    get_langsmith_project_name,
)
from k8s_autopilot.config.settings import Settings


class TestAssembleLangsmithThreadUrl:
    """Tests for _assemble_langsmith_thread_url."""

    def test_basic_url_construction(self):
        url = _assemble_langsmith_thread_url(
            "https://smith.langchain.com/o/org/projects/p/abc123",
            "thread-42",
        )
        assert url.endswith("/t/thread-42?utm_source=k8s-autopilot")

    def test_strips_trailing_slash(self):
        url = _assemble_langsmith_thread_url(
            "https://smith.langchain.com/project/",
            "thread-1",
        )
        assert "/project/t/thread-1" in url
        assert "/project//t/" not in url

    def test_utm_source_present(self):
        url = _assemble_langsmith_thread_url("https://example.com/p", "t1")
        assert "utm_source=k8s-autopilot" in url


class TestGetLangsmithProjectName:
    """Tests for get_langsmith_project_name."""

    def test_returns_none_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            assert get_langsmith_project_name() is None

    def test_returns_none_without_tracing_flag(self):
        with patch.dict(
            os.environ,
            {"LANGSMITH_API_KEY": "lsk-test123"},
            clear=True,
        ):
            assert get_langsmith_project_name() is None

    def test_returns_default_project_name(self):
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_API_KEY": "lsk-test123",
                "LANGSMITH_TRACING": "true",
            },
            clear=True,
        ):
            result = get_langsmith_project_name()
            assert result == "k8s-autopilot"

    def test_respects_custom_project_env_var(self):
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_API_KEY": "lsk-test123",
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_PROJECT": "my-custom-project",
            },
            clear=True,
        ):
            result = get_langsmith_project_name()
            assert result == "my-custom-project"

    def test_langchain_v2_tracing_works(self):
        with patch.dict(
            os.environ,
            {
                "LANGCHAIN_API_KEY": "lsk-test123",
                "LANGCHAIN_TRACING_V2": "true",
                "LANGCHAIN_PROJECT": "production-k8s",
            },
            clear=True,
        ):
            result = get_langsmith_project_name()
            assert result == "production-k8s"


class TestApplyTracingSettings:
    """Tests for apply_tracing_settings."""

    def test_enables_tracing_when_configured(self):
        settings = Settings(
            langchain_api_key="lsv2_pt_12345",
            langchain_tracing=True,
            langchain_project="test-project",
            langchain_endpoint="https://api.smith.langchain.com",
        )
        with patch.dict(os.environ, {}, clear=True):
            res = apply_tracing_settings(settings)
            assert res is True
            assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
            assert os.environ["LANGSMITH_TRACING"] == "true"
            assert os.environ["LANGCHAIN_API_KEY"] == "lsv2_pt_12345"
            assert os.environ["LANGSMITH_API_KEY"] == "lsv2_pt_12345"
            assert os.environ["LANGCHAIN_PROJECT"] == "test-project"
            assert os.environ["LANGSMITH_PROJECT"] == "test-project"
            assert os.environ["LANGCHAIN_ENDPOINT"] == "https://api.smith.langchain.com"
            assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"

    def test_disables_tracing_when_flag_is_false(self):
        settings = Settings(
            langchain_api_key="lsv2_pt_12345",
            langchain_tracing=False,
            langchain_project="test-project",
        )
        with patch.dict(os.environ, {}, clear=True):
            res = apply_tracing_settings(settings)
            assert res is False
            assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
            assert os.environ["LANGSMITH_TRACING"] == "false"
