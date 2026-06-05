"""
HITL: OpenTelemetry approval card content verification.

CRITICAL: otel_annotate_deployment card must warn about "rolling restart" —
without this, operators approve annotations that restart all pods in production.
"""
import pytest
from k8s_autopilot.core.agents.observability.middleware import (
    _build_opentelemetry_approval_description,
    build_opentelemetry_hitl_middleware,
)


@pytest.mark.hitl
class TestOtelProvisionCollector:
    def test_card_shows_signals(self):
        desc = _build_opentelemetry_approval_description(
            "otel_provision_collector",
            {"signals": ["traces", "metrics"], "namespace": "otel", "mode": "auto"},
        )
        assert "traces" in desc.lower()

    def test_card_shows_mode(self):
        desc = _build_opentelemetry_approval_description(
            "otel_provision_collector",
            {"signals": ["traces", "metrics"], "namespace": "otel", "mode": "auto"},
        )
        assert "auto" in desc.lower()


@pytest.mark.hitl
class TestOtelPatchCollector:
    def test_overwrite_true_shows_replace(self):
        desc = _build_opentelemetry_approval_description(
            "otel_patch_collector",
            {"name": "prod-collector", "namespace": "otel", "overwrite": True},
        )
        assert "REPLACE" in desc

    def test_overwrite_false_shows_patch(self):
        desc = _build_opentelemetry_approval_description(
            "otel_patch_collector",
            {"name": "prod-collector", "namespace": "otel", "overwrite": False},
        )
        assert "PATCH" in desc


@pytest.mark.hitl
class TestOtelAnnotateDeployment:
    """
    Most critical OTel HITL test: operator must know pods will restart.
    """

    def test_card_warns_rolling_restart(self):
        desc = _build_opentelemetry_approval_description(
            "otel_annotate_deployment",
            {"name": "checkout", "namespace": "prod"},
        )
        assert "rolling restart" in desc.lower(), (
            "CRITICAL: otel_annotate_deployment card missing 'rolling restart' warning. "
            "Operator will approve without knowing pods will restart in production."
        )


@pytest.mark.hitl
class TestOtelSpanMetrics:
    def test_card_warns_cardinality(self):
        desc = _build_opentelemetry_approval_description(
            "otel_enable_spanmetrics_for_service",
            {"name": "prod-collector", "namespace": "otel"},
        )
        assert "cardinality" in desc.lower()


@pytest.mark.hitl
def test_opentelemetry_hitl_gates_all_6_tools():
    """Every OTel write tool must be gated."""
    middleware = build_opentelemetry_hitl_middleware()
    required_gated = {
        "otel_provision_collector",
        "otel_patch_collector",
        "otel_patch_instrumentation",
        "otel_annotate_deployment",
        "otel_toggle_sampling_strategy",
        "otel_enable_spanmetrics_for_service",
    }
    missing = required_gated - set(middleware.interrupt_on.keys())
    assert not missing, f"OTel tools missing from HITL gate: {missing}"
