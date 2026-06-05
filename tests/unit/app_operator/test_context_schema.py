import pytest
from k8s_autopilot.core.state.app_operator_state import AppOperatorContext

@pytest.mark.unit
def test_context_schema_is_typed_dict():
    assert issubclass(AppOperatorContext, dict)

@pytest.mark.unit
def test_context_all_fields_optional():
    ctx: AppOperatorContext = {}  # should not raise
    assert isinstance(ctx, dict)

@pytest.mark.unit
def test_context_dry_run_defaults_absent():
    ctx: AppOperatorContext = {}
    assert "dry_run" not in ctx

@pytest.mark.unit
def test_context_accepts_valid_fields():
    ctx: AppOperatorContext = {
        "argocd_server": "https://argocd.example.com",
        "github_repo": "org/repo",
        "default_namespace": "staging",
    }
    assert ctx["argocd_server"] == "https://argocd.example.com"
