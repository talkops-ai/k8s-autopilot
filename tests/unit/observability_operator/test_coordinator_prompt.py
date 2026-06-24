"""
Unit: Coordinator prompt regression tests.

Catches silent prompt edits that break routing. The coordinator prompt
is composed via PromptRegistry — these tests verify the critical sections
are present regardless of how the registry composes them.
"""
import pytest
from k8s_autopilot.core.agents.observability.coordinator import OBS_COORDINATOR_PROMPT

@pytest.mark.unit
@pytest.mark.parametrize("subagent_name", [
    "prometheus-operator",
    "alertmanager-operator",
    "opentelemetry-operator",
    "loki-operator",
    "tempo-operator",
])
def test_contains_all_5_subagent_names(subagent_name):
    """Subagent name typo → coordinator can never delegate to that agent."""
    assert subagent_name in OBS_COORDINATOR_PROMPT, (
        f"Coordinator prompt missing subagent name '{subagent_name}'. "
        "Routing table is incomplete — delegation will fail silently."
    )

@pytest.mark.unit
def test_contains_query_classification_categories():
    """out_of_scope category removed → agent processes Helm/K8s requests as in-scope."""
    assert "out_of_scope" in OBS_COORDINATOR_PROMPT

@pytest.mark.unit
def test_contains_plan_locked_protocol():
    """PLAN-LOCKED protocol removed → agent ignores approved plan constraints."""
    assert "PLAN-LOCKED" in OBS_COORDINATOR_PROMPT

@pytest.mark.unit
def test_prompt_is_non_empty_and_substantial():
    """Prompt accidentally cleared or truncated."""
    assert len(OBS_COORDINATOR_PROMPT) > 500, (
        f"Coordinator prompt is only {len(OBS_COORDINATOR_PROMPT)} chars — "
        "likely truncated or empty."
    )
