"""
HITL: Prometheus approval card content verification.

Verifies that the HITL approval card shows the operator all information
needed to make an informed approval/rejection decision for each of the
6 gated Prometheus tools.
"""
import pytest
from k8s_autopilot.core.agents.observability.middleware import (
    _build_prometheus_approval_description,
    build_prometheus_hitl_middleware,
)


@pytest.mark.hitl
class TestPrometheusInstallExporter:
    def test_card_shows_operation_type(self):
        desc = _build_prometheus_approval_description(
            "prom_install_exporter",
            {"exporter_type": "node-exporter", "namespace": "monitoring", "backend_id": "prod"},
        )
        assert "EXPORTER INSTALLATION" in desc

    def test_card_shows_namespace(self):
        desc = _build_prometheus_approval_description(
            "prom_install_exporter",
            {"exporter_type": "node-exporter", "namespace": "monitoring", "backend_id": "prod"},
        )
        assert "monitoring" in desc

    def test_card_shows_backend_id(self):
        desc = _build_prometheus_approval_description(
            "prom_install_exporter",
            {"exporter_type": "node-exporter", "namespace": "monitoring", "backend_id": "prod"},
        )
        assert "prod" in desc


@pytest.mark.hitl
class TestPrometheusUninstallExporter:
    def test_card_shows_removal(self):
        desc = _build_prometheus_approval_description(
            "prom_uninstall_exporter",
            {"exporter_type": "redis-exporter", "namespace": "monitoring"},
        )
        assert "REMOVAL" in desc

    def test_card_shows_exporter_type(self):
        desc = _build_prometheus_approval_description(
            "prom_uninstall_exporter",
            {"exporter_type": "redis-exporter", "namespace": "monitoring"},
        )
        assert "redis-exporter" in desc


@pytest.mark.hitl
class TestPrometheusServiceMonitor:
    def test_card_shows_service_name(self):
        desc = _build_prometheus_approval_description(
            "prom_apply_servicemonitor",
            {"service_name": "checkout", "namespace": "prod", "scrape_interval": "15s"},
        )
        assert "checkout" in desc

    def test_card_shows_scrape_interval(self):
        desc = _build_prometheus_approval_description(
            "prom_apply_servicemonitor",
            {"service_name": "checkout", "namespace": "prod", "scrape_interval": "15s"},
        )
        assert "15s" in desc


@pytest.mark.hitl
class TestPrometheusRuleUpsert:
    def test_card_shows_group_name(self):
        desc = _build_prometheus_approval_description(
            "prom_upsert_rule_group",
            {"group_name": "high-error-rate", "rules": [{"alert": "err"}], "storage_mode": "k8s_crd"},
        )
        assert "high-error-rate" in desc

    def test_card_shows_rule_count(self):
        desc = _build_prometheus_approval_description(
            "prom_upsert_rule_group",
            {"group_name": "high-error-rate", "rules": [{"alert": "err"}], "storage_mode": "k8s_crd"},
        )
        assert "1 rule(s)" in desc

    def test_card_shows_storage_mode(self):
        desc = _build_prometheus_approval_description(
            "prom_upsert_rule_group",
            {"group_name": "x", "rules": [{"alert": "a"}], "storage_mode": "k8s_crd"},
        )
        assert "k8s_crd" in desc


@pytest.mark.hitl
def test_prometheus_hitl_gates_all_6_tools():
    """Every Prometheus write tool must be gated. Missing gate → no approval required."""
    middleware = build_prometheus_hitl_middleware()
    required_gated = {
        "prom_install_exporter",
        "prom_uninstall_exporter",
        "prom_apply_servicemonitor",
        "prom_upsert_rule_group",
        "prom_manage_file_sd",
        "prom_configure_remote_write",
    }
    missing = required_gated - set(middleware.interrupt_on.keys())
    assert not missing, f"Prometheus tools missing from HITL gate: {missing}"
