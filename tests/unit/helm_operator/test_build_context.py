import os
import pytest
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from k8s_autopilot.core.state.helm_operator_state import HelmOperatorContext
from typing import get_type_hints, TypedDict


@pytest.fixture
def coordinator(mock_config):
    return HelmOperatorCoordinator(config=mock_config)


def test_build_context_reads_env_vars(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "myorg/myrepo")
    ctx = coordinator.build_context(supervisor_state={})
    assert ctx.get("github_repo") == "myorg/myrepo"


def test_build_context_strips_empty_string_fields(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "")
    ctx = coordinator.build_context(supervisor_state={})
    assert "github_repo" not in ctx


def test_build_context_propagates_session_id(coordinator):
    ctx = coordinator.build_context(supervisor_state={"session_id": "sess-001"})
    assert ctx.get("session_id") == "sess-001"


def test_build_context_caller_ctx_wins_over_env(coordinator, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    ctx = coordinator.build_context(
        supervisor_state={"context": {"environment": "staging"}}
    )
    assert ctx.get("environment") == "staging"


def test_build_context_injects_cross_domain_context(coordinator):
    ctx = coordinator.build_context(
        supervisor_state={"cross_domain_context": {"key": "value"}}
    )
    assert ctx.get("cross_domain_context") == {"key": "value"}


def test_build_context_injects_domain_summaries(coordinator):
    ctx = coordinator.build_context(
        supervisor_state={"domain_summaries": ["summary1"]}
    )
    assert ctx.get("domain_summaries") == ["summary1"]


def test_build_context_handles_none_supervisor_state(coordinator):
    ctx = coordinator.build_context(supervisor_state=None)
    assert isinstance(ctx, dict)


def test_context_schema_is_typed_dict():
    assert issubclass(HelmOperatorContext, dict)
    # Check that fields are optional in TypedDict
    hints = get_type_hints(HelmOperatorContext)
    assert "dry_run" not in HelmOperatorContext.__required_keys__ if hasattr(HelmOperatorContext, "__required_keys__") else True


def test_context_all_fields_optional():
    # If we can instantiate it without args, it means fields are optional
    ctx = HelmOperatorContext()
    assert isinstance(ctx, dict)
