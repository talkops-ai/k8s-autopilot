"""
Unit: ObsOperationContextMiddleware — context injection and survival.

Bug classes caught:
- Context loss after summarization → agent re-asks for already-performed ops
- Empty log injected as message → confuses model
- Async path diverges from sync → inconsistent behavior
"""
import pytest
import asyncio
from langchain_core.messages import SystemMessage
from k8s_autopilot.core.agents.observability.middleware import ObsOperationContextMiddleware

@pytest.fixture
def middleware():
    return ObsOperationContextMiddleware()

@pytest.mark.unit
def test_injects_system_message_when_ops_log_exists(middleware, obs_ops_log_files):
    state = {"files": obs_ops_log_files}
    result = middleware.before_model(state, None)
    assert result is not None
    assert isinstance(result["messages"][0], SystemMessage)

@pytest.mark.unit
def test_returns_none_when_no_ops_log(middleware):
    state = {"files": {}}
    result = middleware.before_model(state, None)
    assert result is None

@pytest.mark.unit
def test_returns_none_when_ops_log_empty(middleware):
    state = {"files": {"/memories/observability/operations-log.md": {"content": ""}}}
    result = middleware.before_model(state, None)
    assert result is None

@pytest.mark.unit
def test_system_message_contains_ops_context(middleware, obs_ops_log_files):
    state = {"files": obs_ops_log_files}
    result = middleware.before_model(state, None)
    # The ops log fixture has "node-exporter" and "monitoring" — verify they survive
    assert "node-exporter" in result["messages"][0].content
    assert "monitoring" in result["messages"][0].content

@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_delegates_to_sync(middleware, obs_ops_log_files):
    state = {"files": obs_ops_log_files}
    sync_result = middleware.before_model(state, None)
    async_result = await middleware.abefore_model(state, None)
    assert sync_result["messages"][0].content == async_result["messages"][0].content
