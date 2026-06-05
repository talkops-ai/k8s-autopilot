import pytest
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


@pytest.fixture
def coordinator(mock_config):
    return HelmOperatorCoordinator(config=mock_config)


def test_coordinator_prompt_contains_planning_mode(coordinator):
    prompt = coordinator.system_prompt
    # Ensure {planning_mode_section} is properly replaced with actual planning instructions
    assert "{planning_mode_section}" not in prompt
    assert "write_todos" in prompt


def test_coordinator_prompt_includes_workflow_rules(coordinator):
    prompt = coordinator.system_prompt
    assert "Workflow — New Chart" in prompt
    assert "Workflow — Update Chart" in prompt
    assert "Workflow — Helm Operation" in prompt
    assert "CRITICAL: Query Classification" in prompt
    assert "CONVERSATIONAL / END-OF-WORKFLOW" in prompt
    assert "DIFFERENT DOMAIN / OUT-OF-SCOPE TASKS" in prompt
