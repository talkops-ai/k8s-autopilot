import pytest
from k8s_autopilot.core.agents.app_operator.subagents import (
    ARGOCD_ONBOARDER_SUBAGENT,
    ARGO_ROLLOUTS_ONBOARDER_SUBAGENT,
    TRAEFIK_EDGE_ROUTER_SUBAGENT,
    get_app_subagent_specs
)

@pytest.mark.unit
def test_all_subagents_have_required_keys():
    for spec in [ARGOCD_ONBOARDER_SUBAGENT, ARGO_ROLLOUTS_ONBOARDER_SUBAGENT, TRAEFIK_EDGE_ROUTER_SUBAGENT]:
        assert "name" in spec
        assert "description" in spec
        assert "system_prompt" in spec

@pytest.mark.unit
def test_argocd_onboarder_spec_name():
    assert ARGOCD_ONBOARDER_SUBAGENT["name"] == "argocd-onboarder"

@pytest.mark.unit
def test_argo_rollouts_onboarder_spec_name():
    assert ARGO_ROLLOUTS_ONBOARDER_SUBAGENT["name"] == "argo-rollouts-onboarder"

@pytest.mark.unit
def test_traefik_edge_router_spec_name():
    assert TRAEFIK_EDGE_ROUTER_SUBAGENT["name"] == "traefik-edge-router"

@pytest.mark.unit
def test_get_app_subagent_specs_returns_3():
    specs = get_app_subagent_specs(coordinator_model="mock")
    assert len(specs) == 3



@pytest.mark.unit
def test_traefik_prompt_contains_generate_before_apply():
    assert "Generate-before-apply" in TRAEFIK_EDGE_ROUTER_SUBAGENT["system_prompt"]

@pytest.mark.unit
def test_all_subagent_prompts_contain_plan_approved_section():
    for spec in [ARGOCD_ONBOARDER_SUBAGENT, ARGO_ROLLOUTS_ONBOARDER_SUBAGENT, TRAEFIK_EDGE_ROUTER_SUBAGENT]:
        assert "[PLAN-APPROVED]" in spec["system_prompt"]
