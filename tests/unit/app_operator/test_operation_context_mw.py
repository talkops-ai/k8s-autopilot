import pytest
import asyncio
from langchain_core.messages import SystemMessage
from k8s_autopilot.core.agents.app_operator.middleware import AppOperationContextMiddleware

@pytest.fixture
def middleware():
    return AppOperationContextMiddleware()

@pytest.mark.unit
def test_injects_system_message_when_ops_log_exists(middleware, ops_log_files):
    state = {"files": ops_log_files}
    result = middleware.before_model(state, None)
    assert result is not None
    assert isinstance(result["messages"][0], SystemMessage)
    assert "operations-log" not in result["messages"][0].content.lower()

@pytest.mark.unit
def test_returns_none_when_no_ops_log(middleware):
    state = {"files": {}}
    result = middleware.before_model(state, None)
    assert result is None

@pytest.mark.unit
def test_returns_none_when_ops_log_empty(middleware):
    state = {"files": {"/memories/app-operator/operations-log.md": {"content": ""}}}
    result = middleware.before_model(state, None)
    assert result is None

@pytest.mark.unit
def test_system_message_contains_ops_context(middleware, ops_log_files):
    state = {"files": ops_log_files}
    result = middleware.before_model(state, None)
    assert "frontend" in result["messages"][0].content
    assert "staging" in result["messages"][0].content

@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_delegates_to_sync(middleware, ops_log_files):
    state = {"files": ops_log_files}
    sync_result = middleware.before_model(state, None)
    async_result = await middleware.abefore_model(state, None)
    assert sync_result["messages"][0].content == async_result["messages"][0].content
