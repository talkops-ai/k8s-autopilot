"""
HITL: Argo Rollouts approval card content verification.

Tests that the rollout description builder produces correct, operation-specific
content for each mutation type. Each card must contain enough context for
an operator to approve/reject without switching to another tool.
"""
import pytest

from k8s_autopilot.core.agents.app_operator.middleware import _build_rollouts_approval_description


@pytest.mark.hitl
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    # Delete shows rollout name + destructive warning
    ("argo_delete_rollout",
     {"name": "frontend", "namespace": "staging"},
     "frontend"),

    ("argo_delete_rollout",
     {"name": "frontend", "namespace": "staging"},
     "DELETE ROLLOUT"),

    # Migration preview (apply=False) is read-only — shows PREVIEW not PRODUCTION
    ("convert_deployment_to_rollout",
     {"deployment_name": "frontend", "namespace": "staging", "apply": False, "mode": "workloadRef"},
     "PREVIEW"),

    # Migration apply=True is destructive — shows migration warning
    ("convert_deployment_to_rollout",
     {"deployment_name": "frontend", "namespace": "staging", "apply": True, "mode": "workloadRef"},
     "MIGRATION"),

    # promote_full is highest risk — shows 100% traffic warning
    ("argo_manage_rollout_lifecycle",
     {"name": "frontend", "namespace": "staging", "action": "promote_full"},
     "100%"),

    # abort shows traffic restoration message
    ("argo_manage_rollout_lifecycle",
     {"name": "frontend", "namespace": "staging", "action": "abort"},
     "stable"),

    # skip_analysis is highest risk — warns about bypassing Prometheus gates
    ("argo_manage_rollout_lifecycle",
     {"name": "frontend", "namespace": "staging", "action": "skip_analysis"},
     "SKIP ANALYSIS"),

    # New rollout shows strategy + image
    ("argo_create_rollout",
     {"name": "frontend", "namespace": "staging", "strategy": "canary", "image": "nginx:v2"},
     "canary"),
])
def test_rollouts_hitl_card_content(tool_name, tool_args, expected_in_card):
    """Rollout approval card must contain the expected text."""
    card = _build_rollouts_approval_description(tool_name, tool_args)
    assert expected_in_card.lower() in card.lower(), (
        f"Card for '{tool_name}' (args={tool_args}) missing '{expected_in_card}'.\n"
        f"Actual card:\n{card}"
    )


@pytest.mark.hitl
def test_promote_full_different_from_promote():
    """
    promote_full and promote cards must be distinguishable.

    promote_full is irreversible (commits 100% traffic) — its card must be
    clearly different from a normal promote step to prevent confusion.
    """
    full_card = _build_rollouts_approval_description(
        "argo_manage_rollout_lifecycle",
        {"name": "frontend", "namespace": "staging", "action": "promote_full"}
    )
    step_card = _build_rollouts_approval_description(
        "argo_manage_rollout_lifecycle",
        {"name": "frontend", "namespace": "staging", "action": "promote"}
    )
    assert "100%" in full_card, "promote_full card must mention 100% traffic commitment"
    assert "100%" not in step_card, "normal promote card must NOT suggest 100% commitment"


@pytest.mark.hitl
def test_migration_apply_false_shows_no_cluster_changes():
    """apply=False migration card must say 'No cluster changes' to set expectations."""
    card = _build_rollouts_approval_description(
        "convert_deployment_to_rollout",
        {"deployment_name": "frontend", "namespace": "staging", "apply": False}
    )
    assert "no cluster changes" in card.lower(), (
        f"apply=False migration card must state no cluster changes.\nCard:\n{card}"
    )
