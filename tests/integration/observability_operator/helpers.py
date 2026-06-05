"""
Shared test helpers for Observability Operator integration tests.

Re-imports ExhaustingFakeModel and MockSubAgent from app_operator helpers
and adds domain-specific mock subagent factories for the 5 observability
sub-agents.
"""
from langchain_core.messages import AIMessage

# Re-import shared base classes from app_operator
from tests.integration.app_operator.helpers import (  # noqa: F401
    ExhaustingFakeModel,
    MockSubAgent,
    make_mock_subagent,
)


def make_prometheus_subagent(response: str = "Completed Prometheus operation: query executed") -> dict:
    return make_mock_subagent("prometheus-operator", response)


def make_alertmanager_subagent(response: str = "Completed Alertmanager operation: alerts listed") -> dict:
    return make_mock_subagent("alertmanager-operator", response)


def make_otel_subagent(response: str = "Completed OpenTelemetry operation: collector listed") -> dict:
    return make_mock_subagent("opentelemetry-operator", response)


def make_loki_subagent(response: str = "Completed Loki operation: logs queried") -> dict:
    return make_mock_subagent("loki-operator", response)


def make_tempo_subagent(response: str = "Completed Tempo operation: traces queried") -> dict:
    return make_mock_subagent("tempo-operator", response)
