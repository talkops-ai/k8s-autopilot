"""
Unit: Subagent static specs and prompt content verification.

Catches: missing dict keys, name mismatches between coordinator prompt
and subagent spec, missing safety sections in subagent prompts.
"""
import pytest
from k8s_autopilot.core.agents.observability.subagents import (
    PROMETHEUS_OPERATOR_SUBAGENT,
    ALERTMANAGER_OPERATOR_SUBAGENT,
    OPENTELEMETRY_OPERATOR_SUBAGENT,
    LOKI_OPERATOR_SUBAGENT,
    TEMPO_OPERATOR_SUBAGENT,
    get_obs_subagent_specs,
)

ALL_SPECS = [
    PROMETHEUS_OPERATOR_SUBAGENT,
    ALERTMANAGER_OPERATOR_SUBAGENT,
    OPENTELEMETRY_OPERATOR_SUBAGENT,
    LOKI_OPERATOR_SUBAGENT,
    TEMPO_OPERATOR_SUBAGENT,
]

EXPECTED_NAMES = [
    "prometheus-operator",
    "alertmanager-operator",
    "opentelemetry-operator",
    "loki-operator",
    "tempo-operator",
]

@pytest.mark.unit
@pytest.mark.parametrize("spec", ALL_SPECS, ids=[s["name"] for s in ALL_SPECS])
def test_all_specs_have_required_keys(spec):
    """Missing key → _build_mcp_subagent raises KeyError at graph construction."""
    assert "name" in spec
    assert "description" in spec
    assert "system_prompt" in spec

@pytest.mark.unit
@pytest.mark.parametrize("spec,expected_name", zip(ALL_SPECS, EXPECTED_NAMES), ids=EXPECTED_NAMES)
def test_correct_names(spec, expected_name):
    """Name mismatch → coordinator's routing table references a non-existent subagent."""
    assert spec["name"] == expected_name

@pytest.mark.unit
def test_get_obs_subagent_specs_returns_5():
    """Missing subagent → all requests for that domain fail silently."""
    specs = get_obs_subagent_specs(coordinator_model="mock")
    assert len(specs) == 5

@pytest.mark.unit
@pytest.mark.parametrize("spec", ALL_SPECS, ids=[s["name"] for s in ALL_SPECS])
def test_all_prompts_contain_plan_locked(spec):
    """PLAN-LOCKED removed → subagent ignores approved plan constraints."""
    assert "PLAN-LOCKED" in spec["system_prompt"], (
        f"Subagent '{spec['name']}' prompt missing PLAN-LOCKED protocol"
    )

@pytest.mark.unit
@pytest.mark.parametrize("spec", ALL_SPECS, ids=[s["name"] for s in ALL_SPECS])
def test_all_prompts_contain_rejection_protocol(spec):
    """Rejection protocol missing → subagent doesn't know how to handle OOS."""
    assert "Rejection Protocol" in spec["system_prompt"], (
        f"Subagent '{spec['name']}' prompt missing Rejection Protocol"
    )

@pytest.mark.unit
@pytest.mark.parametrize("spec", ALL_SPECS, ids=[s["name"] for s in ALL_SPECS])
def test_all_prompts_are_substantial(spec):
    """Prompt accidentally emptied or truncated."""
    assert len(spec["system_prompt"]) > 200, (
        f"Subagent '{spec['name']}' prompt is only {len(spec['system_prompt'])} chars"
    )
