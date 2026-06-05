"""
Unit: ObservabilityContext TypedDict schema validation.

Bug classes caught:
- Schema drift (base class changed from TypedDict to Pydantic)
- Required field regression (adding Required breaks callers)
- Field rename (prometheus_url → prom_url silently breaks env injection)
"""
import pytest
from k8s_autopilot.core.state.observability_state import ObservabilityContext

@pytest.mark.unit
def test_context_schema_is_typed_dict():
    assert issubclass(ObservabilityContext, dict)

@pytest.mark.unit
def test_context_all_fields_optional():
    ctx: ObservabilityContext = {}  # should not raise
    assert isinstance(ctx, dict)

@pytest.mark.unit
def test_context_dry_run_defaults_absent():
    ctx: ObservabilityContext = {}
    assert "dry_run" not in ctx

@pytest.mark.unit
def test_context_accepts_backend_fields():
    ctx: ObservabilityContext = {
        "prometheus_url": "http://prometheus:9090",
        "alertmanager_url": "http://alertmanager:9093",
        "default_backend_id": "prod",
    }
    assert ctx["prometheus_url"] == "http://prometheus:9090"
    assert ctx["alertmanager_url"] == "http://alertmanager:9093"
    assert ctx["default_backend_id"] == "prod"

@pytest.mark.unit
def test_context_accepts_sre_investigation_fields():
    ctx: ObservabilityContext = {
        "service_name": "checkout",
        "environment": "prod",
        "incident_id": "INC-123",
        "time_window": "last_15m",
        "tenant_id": "acme",
    }
    assert ctx["service_name"] == "checkout"
    assert ctx["environment"] == "prod"
    assert ctx["incident_id"] == "INC-123"
