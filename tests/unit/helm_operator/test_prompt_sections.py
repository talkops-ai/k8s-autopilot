"""
Unit: Helm Operator PromptRegistry — scope bug regression tests.

These tests catch the exact bug class where keyword-based scope rules
cause false OOS rejections for legitimate Helm install requests.
"""
import pytest
from k8s_autopilot.core.agents.helm_operator.prompt_sections import (
    COORDINATOR_SCOPE,
    COORDINATOR_ROUTING_RULES,
    HELM_OPERATION_SCOPE,
    HELM_OPERATION_PLAN_LOCKED_PROTOCOL,
    create_coordinator_registry,
    create_helm_operation_registry,
    compose_coordinator_prompt,
    compose_helm_operation_prompt,
)


# ── Scope bug regression ────────────────────────────────────────────────

@pytest.mark.unit
def test_coordinator_scope_contains_operation_rule():
    """The root cause: scope must classify by operation type, not keywords."""
    assert "operation type determines scope" in COORDINATOR_SCOPE.lower(), (
        "COORDINATOR_SCOPE is missing the critical operation-based scope rule. "
        "Without this, the LLM will keyword-match 'ArgoCD' and reject Helm installs."
    )


@pytest.mark.unit
def test_coordinator_scope_has_disambiguation_examples():
    """Disambiguation examples prevent false OOS for charts named after OOS tools."""
    assert "Install argo-cd chart" in COORDINATOR_SCOPE
    assert "IN SCOPE" in COORDINATOR_SCOPE


@pytest.mark.unit
def test_routing_rules_not_flat_keyword_ban():
    """The routing_rules out_of_scope line must NOT be a flat keyword list."""
    # The old bug: "out_of_scope: ArgoCD, Argo Rollouts, Traefik, ..."
    # This triggers false OOS on "install argo-cd chart"
    assert "out_of_scope: ArgoCD," not in COORDINATOR_ROUTING_RULES, (
        "COORDINATOR_ROUTING_RULES still uses a flat keyword ban. "
        "Use operation-qualified text instead."
    )


@pytest.mark.unit
def test_helm_operation_scope_operation_based():
    """The subagent scope must also be operation-based, not keyword-based."""
    assert "operation type determines scope" in HELM_OPERATION_SCOPE.lower()
    assert "ALWAYS IN SCOPE" in HELM_OPERATION_SCOPE


# ── Registry testability ────────────────────────────────────────────────

@pytest.mark.unit
def test_registry_section_override():
    """Can override any section — proves testability of the registry pattern."""
    custom_scope = "<scope>Custom test scope</scope>"
    registry = create_coordinator_registry(scope=custom_scope)
    prompt = registry.compose()
    assert "Custom test scope" in prompt
    # Original scope should NOT be present
    assert "operation type determines scope" not in prompt.lower()


@pytest.mark.unit
def test_registry_compose_excludes_section():
    """Can exclude sections — useful for slim prompts or A/B testing."""
    registry = create_coordinator_registry()
    prompt = registry.compose(exclude={"safety_guardrails"})
    assert "<safety_and_guardrails>" not in prompt
    # Other sections should still be present
    assert "<identity>" in prompt


@pytest.mark.unit
def test_compose_coordinator_prompt_returns_string():
    """Smoke test: compose_coordinator_prompt returns a non-empty string."""
    prompt = compose_coordinator_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 500


@pytest.mark.unit
def test_compose_helm_operation_prompt_returns_string():
    """Smoke test: compose_helm_operation_prompt returns a non-empty string."""
    prompt = compose_helm_operation_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 500


@pytest.mark.unit
def test_helm_operation_registry_has_expected_sections():
    """Verify all critical sections are registered for helm-operation subagent."""
    registry = create_helm_operation_registry()
    expected = [
        "identity", "context_recovery", "scope",
        "read_only_fast_path", "mcp_resource_rules",
        "workflow_state_modifying", "plan_locked_protocol",
        "safety_rules", "output_contract",
    ]
    for section in expected:
        assert registry.has(section), (
            f"helm-operation registry missing section '{section}'"
        )
