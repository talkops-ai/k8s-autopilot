"""
Integration test fixtures for Observability Operator.

Key design decisions (LangChain agentic testing best practices):
- ExhaustingFakeModel raises RuntimeError instead of cycling when responses are
  exhausted.  This is the ONLY safe fake model for integration tests — it turns
  infinite-loop bugs into immediate test failures.
- model_limit_middleware (ModelCallLimitMiddleware) is a belt-and-suspenders
  backstop wired into every test's coordinator via monkeypatch.
"""
import pytest
from unittest.mock import MagicMock
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage

from k8s_autopilot.core.agents.observability.coordinator import ObservabilityCoordinator

from tests.integration.observability_operator.helpers import (  # noqa: F401
    ExhaustingFakeModel,
    MockSubAgent,
    make_mock_subagent,
    make_prometheus_subagent,
    make_alertmanager_subagent,
    make_otel_subagent,
    make_loki_subagent,
    make_tempo_subagent,
)


@pytest.fixture
def model_limit_middleware():
    """
    CRITICAL safety backstop: prevents ExhaustingFakeModel from cycling.

    Per LangChain agentic testing best practice: always wire this into
    integration tests that use fake models to prevent infinite tool loops.
    """
    return ModelCallLimitMiddleware(run_limit=10, exit_behavior="end")


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.get_llm_config.return_value = {"model": "fake", "temperature": 0}
    config.get_llm_deepagent_config.return_value = {"model": "fake", "temperature": 0}
    return config


@pytest.fixture
def coordinator(mock_config):
    return ObservabilityCoordinator(config=mock_config)


@pytest.fixture
def simple_oos_model():
    """Scripted model that returns a single OOS response and stops."""
    return ExhaustingFakeModel(
        responses=[AIMessage(content="This is outside my scope. Please use the appropriate operator.")]
    )
