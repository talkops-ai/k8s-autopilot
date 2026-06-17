"""
Unit: Coordinator prompt regression tests.

Catches silent prompt edits that break routing. The coordinator prompt
is composed via PromptRegistry — these tests verify the critical sections
are present regardless of how the registry composes them.
"""
import pytest
from k8s_autopilot.core.agents.helm_operator.coordinator import HELM_COORDINATOR_PROMPT


@pytest.mark.unit
@pytest.mark.parametrize("subagent_name", [
    "helm-planner",
    "helm-skill-builder",
    "helm-generator",
    "helm-updater",
    "helm-validator",
    "github-agent",
    "helm-operation",
])
def test_contains_all_subagent_names(subagent_name):
    """Subagent name typo → coordinator can never delegate to that agent."""
    assert subagent_name in HELM_COORDINATOR_PROMPT, (
        f"Coordinator prompt missing subagent name '{subagent_name}'. "
        "Routing table is incomplete — delegation will fail silently."
    )


@pytest.mark.unit
def test_contains_query_classification_categories():
    """out_of_scope category removed → agent processes non-Helm requests as in-scope."""
    assert "out_of_scope" in HELM_COORDINATOR_PROMPT


@pytest.mark.unit
def test_contains_workflow_sections():
    """Workflow sections removed → coordinator can't follow pipeline."""
    assert "<workflow_chart_generation>" in HELM_COORDINATOR_PROMPT
    assert "<workflow_chart_update>" in HELM_COORDINATOR_PROMPT
    assert "<workflow_helm_operation>" in HELM_COORDINATOR_PROMPT


@pytest.mark.unit
def test_prompt_is_non_empty_and_substantial():
    """Prompt accidentally cleared or truncated."""
    assert len(HELM_COORDINATOR_PROMPT) > 500, (
        f"Coordinator prompt is only {len(HELM_COORDINATOR_PROMPT)} chars — "
        "likely truncated or empty."
    )
