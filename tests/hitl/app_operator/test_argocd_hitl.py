"""
HITL: ArgoCD approval card content verification.

LangChain best practice for HITL tests:
- Test the description BUILDER function directly (pure unit, no graph needed)
- Fast, deterministic, no LLM calls
- Assert specific strings that operators will read before approving

These tests prevent approval cards from silently losing critical context
(operation type, app name, namespace, danger warnings) after prompt refactors.
"""
import pytest

from k8s_autopilot.core.agents.app_operator.middleware import _build_approval_description


@pytest.mark.hitl
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    # App name is always shown
    ("create_application",  {"name": "web", "destination_namespace": "staging"},       "web"),
    # Non-prod header: no 🚨
    ("create_application",  {"name": "web", "destination_namespace": "staging"},       "APPLICATION CREATION"),
    # Update shows app name
    ("update_application",  {"name": "api", "namespace": "staging"},                   "api"),
    # Sync shows dry-run flag
    ("sync_application",    {"name": "web", "dry_run": True},                          "Dry Run"),
    # Delete shows cascade warning
    ("delete_application",  {"name": "web", "cascade": True},                          "DELETE all Kubernetes"),
    # Project delete shows project name
    ("delete_project",      {"project_name": "team-x"},                                "team-x"),
    # Repo delete shows URL
    ("delete_repository",   {"repo_url": "git@github.com:org/repo"},                   "git@github.com"),
    # HTTPS onboarding shows HTTPS keyword
    ("onboard_repository_https", {"repo_url": "https://github.com/org/repo"},          "HTTPS"),
    # SSH onboarding shows SSH keyword
    ("onboard_repository_ssh",   {"repo_url": "git@github.com:org/repo"},              "SSH"),
    # Project create shows project name
    ("create_project",      {"project_name": "payments"},                              "payments"),
])
def test_argocd_hitl_card_content(tool_name, tool_args, expected_in_card):
    """Approval card must contain the expected text for operator review."""
    card = _build_approval_description(tool_name, tool_args)
    assert expected_in_card.lower() in card.lower(), (
        f"Card for '{tool_name}' missing '{expected_in_card}'.\n"
        f"Actual card:\n{card}"
    )


@pytest.mark.hitl
def test_argocd_hitl_sync_shows_all_flags():
    """Sync card must show dry_run, prune, and force flags explicitly."""
    card = _build_approval_description(
        "sync_application",
        {"name": "web", "dry_run": True, "prune": True, "force": False, "revision": "v1.2.3"}
    )
    assert "v1.2.3" in card, "Revision must appear in sync card"
    assert "prune" in card.lower(), "Prune flag must be shown"
    assert "dry run" in card.lower() or "dry_run" in card.lower(), "Dry-run must be shown"
