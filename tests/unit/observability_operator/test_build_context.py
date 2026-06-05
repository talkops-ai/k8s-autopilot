"""
Unit: ObservabilityCoordinator.build_context() — env var injection, precedence, and filtering.

This is the most bug-prone method because it reads from 3 sources with layered
precedence: env vars → supervisor state → caller context. Each test targets a
specific production failure mode.
"""
import os
import pytest
from k8s_autopilot.core.agents.observability.coordinator import ObservabilityCoordinator

@pytest.mark.unit
def test_reads_prometheus_url_from_env(coordinator, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prom.dev:9090")
    ctx = coordinator.build_context(supervisor_state={})
    assert ctx["prometheus_url"] == "http://prom.dev:9090"

@pytest.mark.unit
def test_reads_alertmanager_url_from_env(coordinator, monkeypatch):
    monkeypatch.setenv("ALERTMANAGER_BASE_URL", "http://am.dev:9093")
    ctx = coordinator.build_context(supervisor_state={})
    assert ctx["alertmanager_url"] == "http://am.dev:9093"

@pytest.mark.unit
def test_reads_prometheus_fallback_url(coordinator, monkeypatch):
    """PROMETHEUS_URL is the fallback when PROMETHEUS_BASE_URL is not set."""
    monkeypatch.delenv("PROMETHEUS_BASE_URL", raising=False)
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom-fallback:9090")
    ctx = coordinator.build_context(supervisor_state={})
    assert ctx["prometheus_url"] == "http://prom-fallback:9090"

@pytest.mark.unit
def test_strips_empty_string_env_vars(coordinator, monkeypatch):
    """Empty env vars must NOT leak into context — subagent would try to connect to ""."""
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "")
    monkeypatch.setenv("ALERTMANAGER_BASE_URL", "")
    ctx = coordinator.build_context(supervisor_state={})
    assert "prometheus_url" not in ctx
    assert "alertmanager_url" not in ctx

@pytest.mark.unit
def test_strips_empty_cluster_context(coordinator, monkeypatch):
    monkeypatch.setenv("K8S_CONTEXT", "")
    ctx = coordinator.build_context(supervisor_state={})
    assert "cluster_context" not in ctx

@pytest.mark.unit
def test_propagates_session_id(coordinator):
    ctx = coordinator.build_context(supervisor_state={"session_id": "sess-obs-001"})
    assert ctx["session_id"] == "sess-obs-001"

@pytest.mark.unit
def test_propagates_task_id(coordinator):
    ctx = coordinator.build_context(supervisor_state={"task_id": "task-obs-x"})
    assert ctx["task_id"] == "task-obs-x"

@pytest.mark.unit
def test_investigation_mode_activates_on_service_name(coordinator):
    ctx = coordinator.build_context(supervisor_state={"service_name": "checkout"})
    assert ctx["service_name"] == "checkout"

@pytest.mark.unit
def test_investigation_mode_propagates_all_sre_fields(coordinator):
    state = {
        "service_name": "payments",
        "environment": "prod",
        "tenant_id": "acme",
        "time_window": "last_1h",
        "incident_id": "INC-456",
        "user_id": "ops-user",
    }
    ctx = coordinator.build_context(supervisor_state=state)
    assert ctx["service_name"] == "payments"
    assert ctx["environment"] == "prod"
    assert ctx["incident_id"] == "INC-456"
    assert ctx["user_id"] == "ops-user"

@pytest.mark.unit
def test_investigation_mode_false_without_sre_fields(coordinator):
    """SRE investigation fields must NOT appear when none are provided."""
    ctx = coordinator.build_context(supervisor_state={})
    for field in ("service_name", "environment", "tenant_id", "time_window", "incident_id"):
        assert field not in ctx

@pytest.mark.unit
def test_investigation_mode_propagates_additional_labels(coordinator):
    state = {
        "service_name": "checkout",
        "additional_labels": {"team": "platform", "tier": "critical"},
    }
    ctx = coordinator.build_context(supervisor_state=state)
    assert ctx["additional_labels"] == {"team": "platform", "tier": "critical"}

@pytest.mark.unit
def test_caller_context_overrides_env(coordinator, monkeypatch):
    monkeypatch.setenv("K8S_DEFAULT_NAMESPACE", "default")
    ctx = coordinator.build_context(
        supervisor_state={"context": {"default_namespace": "custom-ns"}}
    )
    assert ctx["default_namespace"] == "custom-ns"

@pytest.mark.unit
def test_ignores_none_values_in_caller_ctx(coordinator, monkeypatch):
    """None values in caller context must NOT overwrite valid env vars."""
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prom.valid:9090")
    ctx = coordinator.build_context(
        supervisor_state={"context": {"prometheus_url": None}}
    )
    assert ctx["prometheus_url"] == "http://prom.valid:9090"

@pytest.mark.unit
def test_cross_domain_context_propagated(coordinator):
    ctx = coordinator.build_context(
        supervisor_state={"cross_domain_context": {"helm": "deployed nginx v1.2"}}
    )
    assert ctx["cross_domain_context"]["helm"] == "deployed nginx v1.2"

@pytest.mark.unit
def test_empty_cross_domain_context_ignored(coordinator):
    """Empty cross_domain_context dict must NOT leak into context."""
    ctx = coordinator.build_context(supervisor_state={"cross_domain_context": {}})
    assert "cross_domain_context" not in ctx

@pytest.mark.unit
def test_domain_summaries_propagated(coordinator):
    summaries = [{"domain": "helm", "summary": "nginx deployed"}]
    ctx = coordinator.build_context(supervisor_state={"domain_summaries": summaries})
    assert ctx["domain_summaries"] == summaries

@pytest.mark.unit
def test_handles_none_supervisor_state(coordinator, monkeypatch):
    monkeypatch.setenv("K8S_DEFAULT_NAMESPACE", "test-ns")
    ctx = coordinator.build_context(supervisor_state=None)
    assert ctx["default_namespace"] == "test-ns"
    assert isinstance(ctx, dict)
