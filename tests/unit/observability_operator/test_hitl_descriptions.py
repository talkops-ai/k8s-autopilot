"""
Unit: HITL approval card descriptions and middleware factory tool counts.

This is the highest-value unit test file — HITL card bugs go directly to
production operators making approval decisions.

Bug classes caught:
- Operator approves without knowing what exporter/namespace/backend
- Operator doesn't realize test alert fires to PagerDuty ("REAL" warning missing)
- otel_annotate_deployment doesn't warn about rolling restart
- Tempo dry_run badge shows wrong mode
- New tool added without HITL gate → executes without approval
"""
import pytest
from k8s_autopilot.core.agents.observability.middleware import (
    _build_prometheus_approval_description,
    _build_alertmanager_approval_description,
    _build_opentelemetry_approval_description,
    _build_tempo_approval_description,
    _make_interrupt_config,
    build_prometheus_hitl_middleware,
    build_alertmanager_hitl_middleware,
    build_opentelemetry_hitl_middleware,
    build_tempo_hitl_middleware,
)
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig


# ---------------------------------------------------------------------------
# Prometheus HITL
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("prom_install_exporter", {"exporter_type": "node-exporter", "namespace": "monitoring"}, "EXPORTER INSTALLATION"),
    ("prom_install_exporter", {"exporter_type": "node-exporter", "namespace": "monitoring"}, "monitoring"),
    ("prom_install_exporter", {"exporter_type": "node-exporter", "namespace": "monitoring"}, "node-exporter"),
    ("prom_uninstall_exporter", {"exporter_type": "redis-exporter", "namespace": "monitoring"}, "REMOVAL"),
    ("prom_apply_servicemonitor", {"service_name": "checkout", "namespace": "prod", "scrape_interval": "15s"}, "checkout"),
    ("prom_apply_servicemonitor", {"service_name": "checkout", "namespace": "prod", "scrape_interval": "15s"}, "15s"),
    ("prom_upsert_rule_group", {"group_name": "high-error-rate", "rules": [{"alert": "err"}], "storage_mode": "k8s_crd"}, "high-error-rate"),
    ("prom_upsert_rule_group", {"group_name": "high-error-rate", "rules": [{"alert": "err"}], "storage_mode": "k8s_crd"}, "1 rule(s)"),
    ("prom_upsert_rule_group", {"group_name": "x", "rules": [{"alert": "a"}], "storage_mode": "k8s_crd"}, "k8s_crd"),
    ("prom_manage_file_sd", {"targets": ["10.0.0.1:9090"], "file_sd_path": "/etc/prom/targets.json", "sub_action": "add"}, "10.0.0.1:9090"),
    ("prom_manage_file_sd", {"targets": ["10.0.0.1:9090"], "file_sd_path": "/etc/prom/targets.json", "sub_action": "add"}, "/etc/prom/targets.json"),
    ("prom_configure_remote_write", {"remote_url": "https://cortex.example.com/push"}, "https://cortex.example.com/push"),
])
def test_prometheus_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_prometheus_approval_description(tool_name, tool_args)
    assert expected_in_card in desc, (
        f"Prometheus card for '{tool_name}' missing '{expected_in_card}'.\nActual:\n{desc}"
    )

@pytest.mark.unit
def test_prometheus_unknown_tool_fallback():
    desc = _build_prometheus_approval_description("prom_unknown_tool", {})
    assert "prom_unknown_tool" in desc

@pytest.mark.unit
def test_prometheus_hitl_gates_6_tools():
    hitl = build_prometheus_hitl_middleware()
    assert len(hitl.interrupt_on) == 6


# ---------------------------------------------------------------------------
# Alertmanager HITL
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("am_create_silence", {"matchers": [{"name": "alertname", "value": "HighCPU"}], "duration_minutes": 120}, "HighCPU"),
    ("am_create_silence", {"matchers": [{"name": "alertname", "value": "HighCPU"}], "duration_minutes": 120}, "120"),
    ("am_update_silence", {"silence_id": "sil-abc", "add_minutes": 60}, "sil-abc"),
    ("am_update_silence", {"silence_id": "sil-abc", "add_minutes": 60}, "60 minutes"),
    ("am_expire_silence", {"silence_id": "sil-xyz"}, "reactivate"),
    ("am_push_test_alert", {"alert_labels": {"alertname": "TestPagerDuty"}}, "REAL"),
    ("am_push_test_alert", {"alert_labels": {"alertname": "TestPagerDuty"}}, "TestPagerDuty"),
    ("am_silence_alert", {"scope": "service", "duration_minutes": 30}, "service"),
])
def test_alertmanager_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_alertmanager_approval_description(tool_name, tool_args)
    assert expected_in_card.lower() in desc.lower(), (
        f"Alertmanager card for '{tool_name}' missing '{expected_in_card}'.\nActual:\n{desc}"
    )

@pytest.mark.unit
def test_alertmanager_hitl_gates_5_tools():
    hitl = build_alertmanager_hitl_middleware()
    assert len(hitl.interrupt_on) == 5


# ---------------------------------------------------------------------------
# OpenTelemetry HITL
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("otel_provision_collector", {"signals": ["traces", "metrics"], "namespace": "otel", "mode": "auto"}, "traces"),
    ("otel_provision_collector", {"signals": ["traces", "metrics"], "namespace": "otel", "mode": "auto"}, "auto"),
    ("otel_patch_collector", {"name": "prod-collector", "namespace": "otel", "overwrite": True}, "REPLACE"),
    ("otel_patch_collector", {"name": "prod-collector", "namespace": "otel", "overwrite": False}, "PATCH"),
    ("otel_patch_instrumentation", {"name": "java-instr", "namespace": "payments", "endpoint": "otel-collector:4317"}, "otel-collector:4317"),
    ("otel_annotate_deployment", {"name": "checkout", "namespace": "prod"}, "rolling restart"),
    ("otel_toggle_sampling_strategy", {"name": "prod-collector", "namespace": "otel", "target_mode": "tail_sampling"}, "tail_sampling"),
    ("otel_enable_spanmetrics_for_service", {"name": "prod-collector", "namespace": "otel"}, "cardinality"),
])
def test_opentelemetry_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_opentelemetry_approval_description(tool_name, tool_args)
    assert expected_in_card.lower() in desc.lower(), (
        f"OTel card for '{tool_name}' missing '{expected_in_card}'.\nActual:\n{desc}"
    )

@pytest.mark.unit
def test_opentelemetry_hitl_gates_6_tools():
    hitl = build_opentelemetry_hitl_middleware()
    assert len(hitl.interrupt_on) == 6


# ---------------------------------------------------------------------------
# Tempo HITL
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("tempo_create_operator_cr", {"name": "prod-tempo", "namespace": "tracing", "kind": "TempoStack", "storage_type": "s3", "retention": "48h", "dry_run": True}, "TempoStack"),
    ("tempo_create_operator_cr", {"name": "prod-tempo", "namespace": "tracing", "kind": "TempoStack", "storage_type": "s3", "retention": "48h", "dry_run": True}, "s3"),
    ("tempo_create_operator_cr", {"name": "prod-tempo", "namespace": "tracing", "kind": "TempoStack", "storage_type": "s3", "retention": "48h", "dry_run": True}, "48h"),
    ("tempo_patch_operator_cr", {"name": "prod-tempo", "namespace": "tracing", "retention": "72h"}, "72h"),
])
def test_tempo_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_tempo_approval_description(tool_name, tool_args)
    assert expected_in_card in desc, (
        f"Tempo card for '{tool_name}' missing '{expected_in_card}'.\nActual:\n{desc}"
    )

@pytest.mark.unit
def test_tempo_dry_run_badge_true():
    """Operator must know it's a DRY RUN — not live."""
    desc = _build_tempo_approval_description("tempo_create_operator_cr", {"dry_run": True})
    assert "DRY RUN" in desc

@pytest.mark.unit
def test_tempo_dry_run_badge_false():
    """CRITICAL: Operator must know it's a LIVE APPLY — not dry-run."""
    desc = _build_tempo_approval_description("tempo_create_operator_cr", {"dry_run": False})
    assert "LIVE APPLY" in desc

@pytest.mark.unit
def test_tempo_hitl_gates_2_tools():
    hitl = build_tempo_hitl_middleware()
    assert len(hitl.interrupt_on) == 2


# ---------------------------------------------------------------------------
# Shared helper: _make_interrupt_config
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_make_interrupt_config_returns_InterruptOnConfig():
    res = _make_interrupt_config("test", lambda x, y: "test")
    assert isinstance(res, (dict, InterruptOnConfig))

@pytest.mark.unit
def test_make_interrupt_config_default_decisions():
    res = _make_interrupt_config("test", lambda x, y: "test")
    decisions = res.get("allowed_decisions") if isinstance(res, dict) else res.allowed_decisions
    assert decisions == ["approve", "reject"]

@pytest.mark.unit
def test_make_interrupt_config_custom_decisions():
    res = _make_interrupt_config("test", lambda x, y: "test", allowed_decisions=["approve", "edit", "reject"])
    decisions = res.get("allowed_decisions") if isinstance(res, dict) else res.allowed_decisions
    assert decisions == ["approve", "edit", "reject"]
