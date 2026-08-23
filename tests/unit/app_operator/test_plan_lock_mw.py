import pytest
from langchain_core.messages import SystemMessage
from k8s_autopilot.core.middleware.plan_lock import PlanLockMiddleware

@pytest.fixture
def middleware():
    return PlanLockMiddleware()

@pytest.mark.unit
def test_injects_plan_when_active_plan_exists(middleware):
    state = {"files": {"/plan/active-plan.md": {"content": "Create app X"}}}
    result = middleware.before_model(state, None)
    assert result is not None
    assert isinstance(result["messages"][0], SystemMessage)
    assert "Create app X" in result["messages"][0].content

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
def test_plan_message_contains_plan_content(middleware, plan_locked_files):
    state = {"files": plan_locked_files}
    result = middleware.before_model(state, None)
    assert "api-service" in result["messages"][0].content
    assert "production" in result["messages"][0].content

@pytest.mark.unit
def test_handles_plan_as_string_not_dict(middleware):
    state = {"files": {"/plan/active-plan.md": "raw plan text"}}
    result = middleware.before_model(state, None)
    assert "raw plan text" in result["messages"][0].content

@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_delegates_to_sync(middleware, plan_locked_files):
    state = {"files": plan_locked_files}
    sync_result = middleware.before_model(state, None)
    async_result = await middleware.abefore_model(state, None)
    assert sync_result["messages"][0].content == async_result["messages"][0].content


@pytest.mark.unit
def test_todos_formatting_with_title(middleware):
    state = {
        "todos": [
            {"title": "Task 1", "status": "pending"},
            {"title": "Task 2", "status": "completed"}
        ]
    }
    result = middleware.before_model(state, None)
    assert result is not None
    content = result["messages"][0].content
    assert "⏳ Task 1 (pending)" in content
    assert "✅ Task 2 (completed)" in content


@pytest.mark.unit
def test_todos_formatting_with_content_fallback(middleware):
    state = {
        "todos": [
            {"content": "Task 1 Content", "status": "pending"},
            {"content": "Task 2 Content", "status": "completed"}
        ]
    }
    result = middleware.before_model(state, None)
    assert result is not None
    content = result["messages"][0].content
    assert "⏳ Task 1 Content (pending)" in content
    assert "✅ Task 2 Content (completed)" in content

