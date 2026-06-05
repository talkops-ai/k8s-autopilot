import pytest

@pytest.mark.unit
def test_prompt_contains_planning_section(coordinator):
    prompt = coordinator.system_prompt
    assert "{planning_mode_section}" not in prompt

@pytest.mark.unit
def test_prompt_lists_all_three_subagents(coordinator):
    prompt = coordinator.system_prompt
    assert "argocd-onboarder" in prompt
    assert "argo-rollouts-onboarder" in prompt
    assert "traefik-edge-router" in prompt

@pytest.mark.unit
def test_prompt_contains_intent_translation_table(coordinator):
    prompt = coordinator.system_prompt
    assert "Deploy my app" in prompt
    assert "Zero downtime" in prompt

@pytest.mark.unit
def test_prompt_contains_query_classification(coordinator):
    prompt = coordinator.system_prompt
    assert "CONVERSATIONAL" in prompt
    assert "OUT-OF-SCOPE" in prompt
    assert "READ-ONLY" in prompt
    assert "STATE-MODIFYING" in prompt

@pytest.mark.unit
def test_prompt_contains_formatting_section(coordinator):
    prompt = coordinator.system_prompt
    assert "request_chat_continue" in prompt

@pytest.mark.unit
def test_prompt_contains_step_budget(coordinator):
    prompt = coordinator.system_prompt
    assert "150 total" in prompt or "step budget" in prompt.lower()

@pytest.mark.unit
def test_prompt_contains_plan_locked_protocol(coordinator):
    prompt = coordinator.system_prompt
    assert "PLAN-LOCKED" in prompt
