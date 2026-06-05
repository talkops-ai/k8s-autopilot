import os
import pytest
from k8s_autopilot.core.agents.app_operator.coordinator import AppOperatorCoordinator

@pytest.mark.unit
def test_build_context_reads_env_vars(coordinator, monkeypatch):
    monkeypatch.setenv("ARGOCD_SERVER", "https://argo.dev")
    ctx = coordinator.build_context(supervisor_state={})
    assert ctx["argocd_server"] == "https://argo.dev"

@pytest.mark.unit
def test_build_context_reads_github_repo_env(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "myorg/myrepo")
    ctx = coordinator.build_context(supervisor_state={})
    assert ctx["github_repo"] == "myorg/myrepo"

@pytest.mark.unit
def test_build_context_strips_empty_string_fields(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "")
    monkeypatch.setenv("ARGOCD_SERVER", "")
    ctx = coordinator.build_context(supervisor_state={})
    assert "github_repo" not in ctx
    assert "argocd_server" not in ctx

@pytest.mark.unit
def test_build_context_strips_argocd_server_empty(coordinator, monkeypatch):
    monkeypatch.setenv("ARGOCD_SERVER", "")
    ctx = coordinator.build_context(supervisor_state={})
    assert "argocd_server" not in ctx

@pytest.mark.unit
def test_build_context_propagates_session_id(coordinator):
    ctx = coordinator.build_context(supervisor_state={"session_id": "sess-001"})
    assert ctx["session_id"] == "sess-001"

@pytest.mark.unit
def test_build_context_propagates_task_id(coordinator):
    ctx = coordinator.build_context(supervisor_state={"task_id": "task-x"})
    assert ctx["task_id"] == "task-x"

@pytest.mark.unit
def test_build_context_caller_ctx_wins_over_env(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_BRANCH", "main")
    ctx = coordinator.build_context(supervisor_state={"context": {"github_branch": "develop"}})
    assert ctx["github_branch"] == "develop"

@pytest.mark.unit
def test_build_context_handles_none_supervisor_state(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_BRANCH", "test")
    ctx = coordinator.build_context(supervisor_state=None)
    assert ctx["github_branch"] == "test"
    assert isinstance(ctx, dict)

@pytest.mark.unit
def test_build_context_injects_cross_domain_context(coordinator):
    ctx = coordinator.build_context(
        supervisor_state={"cross_domain_context": {"helm": "deployed nginx v1.2"}}
    )
    assert ctx["cross_domain_context"]["helm"] == "deployed nginx v1.2"

@pytest.mark.unit
def test_build_context_ignores_empty_cross_domain(coordinator):
    ctx = coordinator.build_context(supervisor_state={"cross_domain_context": {}})
    assert "cross_domain_context" not in ctx

@pytest.mark.unit
def test_build_context_injects_domain_summaries(coordinator):
    domain_summaries = [{"domain": "helm", "summary": "test"}]
    ctx = coordinator.build_context(supervisor_state={"domain_summaries": domain_summaries})
    assert ctx["domain_summaries"] == domain_summaries

@pytest.mark.unit
def test_build_context_ignores_none_values_in_caller_ctx(coordinator, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "org/repo")
    ctx = coordinator.build_context(supervisor_state={"context": {"github_repo": None}})
    assert ctx["github_repo"] == "org/repo"
