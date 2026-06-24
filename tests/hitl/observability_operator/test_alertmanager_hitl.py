"""
HITL: Alertmanager approval card content verification.

CRITICAL: am_push_test_alert card must contain "REAL" warning — without this,
operators approve test alerts that fire to PagerDuty in production.
"""
import pytest
from k8s_autopilot.core.agents.observability.middleware import (
    _build_alertmanager_approval_description,
    build_alertmanager_hitl_middleware,
)


@pytest.mark.hitl
class TestAlertmanagerCreateSilence:
    def test_card_shows_matchers(self):
        desc = _build_alertmanager_approval_description(
            "am_create_silence",
            {"matchers": [{"name": "alertname", "value": "HighCPU"}], "duration_minutes": 120},
        )
        assert "HighCPU" in desc

    def test_card_shows_duration(self):
        desc = _build_alertmanager_approval_description(
            "am_create_silence",
            {"matchers": [{"name": "alertname", "value": "HighCPU"}], "duration_minutes": 120},
        )
        assert "120" in desc


@pytest.mark.hitl
class TestAlertmanagerExpireSilence:
    def test_card_warns_alerts_reactivate(self):
        desc = _build_alertmanager_approval_description(
            "am_expire_silence",
            {"silence_id": "sil-xyz"},
        )
        assert "reactivate" in desc.lower()


@pytest.mark.hitl
class TestAlertmanagerPushTestAlert:
    """
    Most critical HITL test: operators must see "REAL" warning for test alerts.
    Without this, a test alert fires to PagerDuty and wakes up on-call engineers.
    """

    def test_card_warns_real_alert(self):
        desc = _build_alertmanager_approval_description(
            "am_push_test_alert",
            {"alert_labels": {"alertname": "TestPagerDuty"}},
        )
        assert "REAL" in desc, (
            "CRITICAL: am_push_test_alert card missing 'REAL' warning. "
            "Operator will approve without knowing test alert fires to PagerDuty."
        )

    def test_card_shows_alert_name(self):
        desc = _build_alertmanager_approval_description(
            "am_push_test_alert",
            {"alert_labels": {"alertname": "TestPagerDuty"}},
        )
        assert "TestPagerDuty" in desc


@pytest.mark.hitl
def test_alertmanager_hitl_gates_all_5_tools():
    """Every Alertmanager write tool must be gated."""
    middleware = build_alertmanager_hitl_middleware()
    required_gated = {
        "am_create_silence",
        "am_update_silence",
        "am_expire_silence",
        "am_push_test_alert",
        "am_silence_alert",
    }
    missing = required_gated - set(middleware.interrupt_on.keys())
    assert not missing, f"Alertmanager tools missing from HITL gate: {missing}"
