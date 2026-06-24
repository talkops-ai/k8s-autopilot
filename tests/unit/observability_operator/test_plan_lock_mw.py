"""
Unit: PlanLockMiddleware with observability-specific plan fixtures.

Reuses PlanLockMiddleware from app_operator — verifies it works
with observability domain content (monitoring namespace, exporters).
"""
import pytest
from langchain_core.messages import SystemMessage
from k8s_autopilot.core.agents.app_operator.middleware import PlanLockMiddleware

@pytest.fixture
def middleware():
    return PlanLockMiddleware()

@pytest.mark.unit
def test_injects_plan_when_active_plan_exists(middleware):
    state = {"files": {"/plan/active-plan.md": {"content": "Install node-exporter"}}}
    result = middleware.before_model(state, None)
    assert result is not None
    assert isinstance(result["messages"][0], SystemMessage)
    assert "Install node-exporter" in result["messages"][0].content

@pytest.mark.unit
def test_returns_none_when_no_plan(middleware):
    state = {"files": {}}
    result = middleware.before_model(state, None)
    assert result is None

@pytest.mark.unit
def test_returns_none_when_plan_is_empty_string(middleware):
    state = {"files": {"/plan/active-plan.md": {"content": ""}}}
    result = middleware.before_model(state, None)
    assert result is None

@pytest.mark.unit
def test_plan_message_contains_locked_directive(middleware, plan_locked_files):
    state = {"files": plan_locked_files}
    result = middleware.before_model(state, None)
    assert "LOCKED — DO NOT DEVIATE" in result["messages"][0].content

@pytest.mark.unit
def test_plan_message_contains_observability_content(middleware, plan_locked_files):
    """Verify observability-specific plan content is preserved."""
    state = {"files": plan_locked_files}
    result = middleware.before_model(state, None)
    assert "node-exporter" in result["messages"][0].content
    assert "monitoring" in result["messages"][0].content

@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_delegates_to_sync(middleware, plan_locked_files):
    state = {"files": plan_locked_files}
    sync_result = middleware.before_model(state, None)
    async_result = await middleware.abefore_model(state, None)
    assert sync_result["messages"][0].content == async_result["messages"][0].content
