import pytest
from k8s_autopilot.core.agents.helm_operator.subagents import get_helm_subagent_specs


def test_all_static_subagents_have_required_keys(mock_config):
    # Dummy models for testing specs structure
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    static_specs = [s for s in specs if isinstance(s, dict) and "system_prompt" in s]
    assert len(static_specs) >= 4
    for spec in static_specs:
        assert "name" in spec
        assert "description" in spec
        assert "system_prompt" in spec


def test_helm_skill_builder_spec_name(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    assert any(s.get("name") == "helm-skill-builder" for s in specs if isinstance(s, dict))


def test_helm_generator_spec_name(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    assert any(s.get("name") == "helm-generator" for s in specs if isinstance(s, dict))


def test_helm_validator_spec_name(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    assert any(s.get("name") == "helm-validator" for s in specs if isinstance(s, dict))


def test_helm_updater_spec_name(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    assert any(s.get("name") == "helm-updater" for s in specs if isinstance(s, dict))


def test_github_agent_spec_name(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    names = [s.name if hasattr(s, "name") else s.get("name") for s in specs]
    assert "github-agent" in names


def test_helm_operation_spec_name(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    names = [s.name if hasattr(s, "name") else s.get("name") for s in specs]
    assert "helm-operation" in names


def test_get_helm_subagent_specs_returns_expected_count(mock_config):
    specs = get_helm_subagent_specs(coordinator_model=None, validator_model=None)
    assert len(specs) >= 6
