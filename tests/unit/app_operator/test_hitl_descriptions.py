import pytest
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
from k8s_autopilot.core.agents.app_operator.middleware import (
    _build_approval_description,
    _build_rollouts_approval_description,
    _build_traefik_approval_description,
    build_app_operator_hitl_middleware,
    build_argo_rollouts_hitl_middleware,
    build_traefik_hitl_middleware,
    _is_production_namespace,
    _make_interrupt_config
)

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("create_application", {"name": "web", "project": "default", "destination_namespace": "staging"}, "web"),
    ("create_application", {"name": "web", "project": "default", "destination_namespace": "staging"}, "staging"),
    ("create_application", {"name": "web", "project": "default", "destination_namespace": "staging"}, "APPLICATION CREATION"),
    ("update_application", {"name": "api"}, "UPDATE"),
    ("sync_application", {"name": "web", "dry_run": True}, "**Dry Run**: yes"),
    ("delete_application", {"name": "web", "cascade": True}, "DELETE all Kubernetes"),
    ("delete_application", {"name": "web", "cascade": False}, "orphaned"),
    ("delete_project", {"project_name": "team-x"}, "team-x"),
    ("delete_repository", {"repo_url": "git@github.com:org/repo"}, "git@github.com"),
    ("onboard_repository_https", {"repo_url": "https://github.com/org/repo"}, "HTTPS"),
    ("onboard_repository_ssh", {"repo_url": "git@github.com:org/repo"}, "SSH"),
    ("create_project", {"project_name": "payments"}, "payments"),
])
def test_argocd_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_approval_description(tool_name, tool_args)
    assert expected_in_card in desc

@pytest.mark.unit
def test_create_application_prod_warning():
    desc = _build_approval_description("create_application", {"destination_namespace": "production"})
    assert "🚨 **PRODUCTION" in desc

@pytest.mark.unit
def test_sync_application_prune_warning():
    desc = _build_approval_description("sync_application", {"prune": True})
    assert "orphaned resources will be deleted" in desc

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("argo_delete_rollout", {"name": "web", "namespace": "canary-demo"}, "DELETE ROLLOUT"),
    ("argo_delete_rollout", {"name": "web", "namespace": "canary-demo"}, "canary-demo"),
    ("argo_delete_experiment", {"name": "exp", "namespace": "default"}, "DELETE EXPERIMENT"),
    ("convert_deployment_to_rollout", {"apply": False}, "PREVIEW** (read-only)"),
    ("convert_deployment_to_rollout", {"apply": True, "mode": "workloadRef"}, "workloadRef"),
    ("argo_manage_rollout_lifecycle", {"action": "promote_full"}, "100% traffic"),
    ("argo_manage_rollout_lifecycle", {"action": "promote_full"}, "irreversible"),
    ("argo_manage_rollout_lifecycle", {"action": "abort"}, "stable"),
    ("argo_manage_rollout_lifecycle", {"action": "skip_analysis"}, "HIGHEST RISK"),
    ("argo_manage_rollout_lifecycle", {"action": "skip_analysis"}, "bypasses Prometheus"),
    ("argo_manage_rollout_lifecycle", {"action": "promote"}, "advance to next"),
    ("argo_create_rollout", {"strategy": "canary"}, "canary"),
    ("argo_configure_analysis_template", {"mode": "generate_yaml"}, "PREVIEW"),
    ("argo_configure_analysis_template", {"mode": "execute"}, "Prometheus-backed"),
    ("argo_manage_legacy_deployment", {"action": "generate_scale_down_manifest"}, "scale replicas to 0"),
])
def test_rollouts_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_rollouts_approval_description(tool_name, tool_args)
    assert expected_in_card in desc

@pytest.mark.unit
def test_update_rollout_description():
    desc = _build_rollouts_approval_description("argo_update_rollout", {})
    assert "Approval required for Argo Rollouts operation: argo_update_rollout" in desc or desc

@pytest.mark.unit
def test_convert_deployment_prod():
    desc = _build_rollouts_approval_description("convert_deployment_to_rollout", {"namespace": "prod", "apply": True})
    assert "🚨 **PRODUCTION" in desc

@pytest.mark.unit
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    ("traefik_manage_weighted_routing", {"stable_weight": 80, "canary_weight": 20}, "80"),
    ("traefik_manage_weighted_routing", {"stable_weight": 80, "canary_weight": 20}, "20"),
    ("traefik_manage_weighted_routing", {"action": "delete"}, "traffic through this route will stop"),
    ("traefik_manage_simple_route", {"action": "create"}, "SIMPLE ROUTE"),
    ("traefik_manage_middleware", {"middleware_type": "rateLimit"}, "rateLimit"),
    ("traefik_nginx_migration", {"action": "generate"}, "PREVIEW** (read-only)"),
    ("traefik_nginx_migration", {"action": "apply"}, "MCP_ALLOW_WRITE"),
    ("traefik_nginx_migration", {"action": "revert"}, "restores original"),
    ("traefik_manage_tcp_routing", {"action": "create"}, "no rollback"),
    ("traefik_configure_service_affinity", {"action": "enable"}, "pinned"),
    ("traefik_configure_service_affinity", {"action": "disable"}, "Stateful sessions may be lost"),
])
def test_traefik_approval_description(tool_name, tool_args, expected_in_card):
    desc = _build_traefik_approval_description(tool_name, tool_args)
    assert expected_in_card in desc

@pytest.mark.unit
def test_generate_routing_manifest():
    desc = _build_traefik_approval_description("traefik_generate_routing_manifest", {})
    assert desc is not None

@pytest.mark.unit
def test_argocd_hitl_gates_9_tools():
    hitl = build_app_operator_hitl_middleware()
    assert len(hitl.interrupt_on) == 9

@pytest.mark.unit
def test_rollouts_hitl_gates_10_tools():
    hitl = build_argo_rollouts_hitl_middleware()
    assert len(hitl.interrupt_on) == 10

@pytest.mark.unit
def test_traefik_hitl_gates_7_tools():
    hitl = build_traefik_hitl_middleware()
    assert len(hitl.interrupt_on) == 7

@pytest.mark.unit
def test_argocd_create_app_allows_edit():
    hitl = build_app_operator_hitl_middleware()
    cfg = hitl.interrupt_on["create_application"]
    assert "edit" in cfg["allowed_decisions"]

@pytest.mark.unit
def test_argocd_delete_app_no_edit():
    hitl = build_app_operator_hitl_middleware()
    cfg = hitl.interrupt_on["delete_application"]
    assert "edit" not in cfg["allowed_decisions"]
    assert "approve" in cfg["allowed_decisions"]
    assert "reject" in cfg["allowed_decisions"]

@pytest.mark.unit
def test_production_namespace_detection():
    assert _is_production_namespace("prod") is True
    assert _is_production_namespace("production") is True
    assert _is_production_namespace("live") is True
    assert _is_production_namespace("prd") is True

@pytest.mark.unit
def test_non_production_namespace():
    assert _is_production_namespace("staging") is False
    assert _is_production_namespace("default") is False

@pytest.mark.unit
def test_production_namespace_case_insensitive():
    assert _is_production_namespace("PRODUCTION") is True

@pytest.mark.unit
def test_make_interrupt_config_returns_InterruptOnConfig():
    res = _make_interrupt_config("test", lambda x, y: "test")
    assert isinstance(res, dict)

@pytest.mark.unit
def test_make_interrupt_config_default_decisions():
    res = _make_interrupt_config("test", lambda x, y: "test")
    assert res.get("allowed_decisions") == ["approve", "reject"]

@pytest.mark.unit
def test_make_interrupt_config_custom_decisions():
    res = _make_interrupt_config("test", lambda x, y: "test", allowed_decisions=["approve", "edit", "reject"])
    assert res.get("allowed_decisions") == ["approve", "edit", "reject"]
