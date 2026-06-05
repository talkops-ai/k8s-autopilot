"""
HITL: Tempo approval card content verification.

CRITICAL: dry_run badge must distinguish "🔍 DRY RUN" from "⚡ LIVE APPLY".
Bug: dry_run defaults to True in the builder but the tool might not pass it,
so the operator thinks it's dry-run when it's actually live.
"""
import pytest
from k8s_autopilot.core.agents.observability.middleware import (
    _build_tempo_approval_description,
    build_tempo_hitl_middleware,
)


@pytest.mark.hitl
class TestTempoCreateCR:
    def test_card_shows_kind(self):
        desc = _build_tempo_approval_description(
            "tempo_create_operator_cr",
            {"name": "prod-tempo", "namespace": "tracing", "kind": "TempoStack",
             "storage_type": "s3", "retention": "48h", "dry_run": True},
        )
        assert "TempoStack" in desc

    def test_card_shows_storage(self):
        desc = _build_tempo_approval_description(
            "tempo_create_operator_cr",
            {"name": "prod-tempo", "namespace": "tracing", "kind": "TempoStack",
             "storage_type": "s3", "retention": "48h", "dry_run": True},
        )
        assert "s3" in desc

    def test_card_shows_retention(self):
        desc = _build_tempo_approval_description(
            "tempo_create_operator_cr",
            {"name": "prod-tempo", "namespace": "tracing", "kind": "TempoStack",
             "storage_type": "s3", "retention": "48h", "dry_run": True},
        )
        assert "48h" in desc


@pytest.mark.hitl
class TestTempoDryRunBadge:
    """Most critical Tempo test: operator must know if it's dry-run or live."""

    def test_dry_run_true_shows_badge(self):
        desc = _build_tempo_approval_description(
            "tempo_create_operator_cr",
            {"dry_run": True},
        )
        assert "DRY RUN" in desc, "Operator must see DRY RUN badge when dry_run=True"

    def test_dry_run_false_shows_live(self):
        desc = _build_tempo_approval_description(
            "tempo_create_operator_cr",
            {"dry_run": False},
        )
        assert "LIVE APPLY" in desc, (
            "CRITICAL: dry_run=False must show LIVE APPLY badge. "
            "Without this, operator thinks it's a dry-run when it's live."
        )

    def test_dry_run_missing_defaults_safely(self):
        """When dry_run is not passed at all, card must not claim LIVE APPLY."""
        desc = _build_tempo_approval_description(
            "tempo_create_operator_cr",
            {},
        )
        # Should default to dry_run=True (safe default) or show a warning
        assert "LIVE APPLY" not in desc or "DRY RUN" in desc


@pytest.mark.hitl
class TestTempoPatchCR:
    def test_card_shows_patch_fields(self):
        desc = _build_tempo_approval_description(
            "tempo_patch_operator_cr",
            {"name": "prod-tempo", "namespace": "tracing", "retention": "72h"},
        )
        assert "72h" in desc


@pytest.mark.hitl
def test_tempo_hitl_gates_all_2_tools():
    """Both Tempo write tools must be gated."""
    middleware = build_tempo_hitl_middleware()
    required_gated = {
        "tempo_create_operator_cr",
        "tempo_patch_operator_cr",
    }
    missing = required_gated - set(middleware.interrupt_on.keys())
    assert not missing, f"Tempo tools missing from HITL gate: {missing}"
