"""
Integration: Cross-domain context propagation and SRE investigation mode.

Tests that supervisor state (cross_domain_context, domain_summaries,
SRE fields) correctly propagates through build_context into the
coordinator's runtime context.
"""
import pytest
from k8s_autopilot.core.agents.observability.coordinator import ObservabilityCoordinator


@pytest.mark.integration
def test_cross_domain_context_reaches_build_context(coordinator):
    ctx = coordinator.build_context(
        supervisor_state={"cross_domain_context": {"helm": "nginx v1.2 deployed"}}
    )
    assert "cross_domain_context" in ctx
    assert ctx["cross_domain_context"]["helm"] == "nginx v1.2 deployed"

@pytest.mark.integration
def test_domain_summaries_propagated(coordinator):
    summaries = [
        {"domain": "helm", "summary": "nginx deployed"},
        {"domain": "app", "summary": "ArgoCD app synced"},
    ]
    ctx = coordinator.build_context(supervisor_state={"domain_summaries": summaries})
    assert ctx["domain_summaries"] == summaries
    assert len(ctx["domain_summaries"]) == 2

@pytest.mark.integration
def test_ops_context_survives_build_context(coordinator, monkeypatch):
    """Verify operations context is injected from the ops log file in state."""
    from k8s_autopilot.core.agents.observability.middleware import ObsOperationContextMiddleware
    mw = ObsOperationContextMiddleware()
    state = {
        "files": {
            "/memories/observability/operations-log.md": {
                "content": (
                    "# Observability Operations Journal\n\n"
                    "### INSTALL (2026-06-04 10:00 UTC)\n"
                    "- **Resource**: `node-exporter`\n"
                )
            }
        }
    }
    result = mw.before_model(state, None)
    assert result is not None
    assert "node-exporter" in result["messages"][0].content

@pytest.mark.integration
def test_investigation_mode_injects_sre_fields(coordinator):
    state = {
        "service_name": "payments",
        "environment": "prod",
        "incident_id": "INC-789",
        "time_window": "last_30m",
    }
    ctx = coordinator.build_context(supervisor_state=state)
    assert ctx["service_name"] == "payments"
    assert ctx["environment"] == "prod"
    assert ctx["incident_id"] == "INC-789"
    assert ctx["time_window"] == "last_30m"

@pytest.mark.integration
def test_investigation_mode_inactive_without_fields(coordinator):
    """When no SRE fields are present, investigation context must NOT be injected."""
    ctx = coordinator.build_context(supervisor_state={"session_id": "s1"})
    for field in ("service_name", "environment", "tenant_id", "time_window", "incident_id"):
        assert field not in ctx
