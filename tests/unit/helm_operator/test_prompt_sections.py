"""
Unit: Helm Operator Prompt — scope bug regression tests.

These tests catch the exact bug class where keyword-based scope rules
cause false OOS rejections for legitimate Helm install requests.
"""
import pytest
import re
from pathlib import Path
from k8s_autopilot.core.backend import get_project_root


def _parse_xml_block(content: str, tag: str) -> str:
    """Helper to extract XML-tagged section from content."""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
    return match.group(0).strip() if match else ""


# Load the actual files for scope assertions
root = get_project_root()
coord_file = root / "plugins" / "helm-operator" / "prompts" / "coordinator.md"
coord_content = coord_file.read_text(encoding="utf-8") if coord_file.exists() else ""

COORDINATOR_SCOPE = _parse_xml_block(coord_content, "scope")
COORDINATOR_ROUTING_RULES = _parse_xml_block(coord_content, "routing_rules")

op_file = root / "plugins" / "helm-operator" / "agents" / "helm-operation" / "prompts" / "system.md"
op_content = op_file.read_text(encoding="utf-8") if op_file.exists() else ""

HELM_OPERATION_SCOPE = _parse_xml_block(op_content, "scope")


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
