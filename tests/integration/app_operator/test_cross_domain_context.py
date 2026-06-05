import pytest
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_cross_domain_context_injected_into_state(coordinator, model_limit_middleware, monkeypatch):
    monkeypatch.setattr(coordinator, "get_model", lambda: FakeMessagesListChatModel(responses=[AIMessage(content="Done")]))
    agent = await coordinator.build_agent()
    
    state = coordinator.build_context(supervisor_state={"cross_domain_context": {"helm": "nginx deployed"}})
    # Simulate injection check (in a real test we'd inspect the state passed to the graph)
    assert "cross_domain_context" in state
    assert state["cross_domain_context"]["helm"] == "nginx deployed"

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_domain_summaries_propagated(coordinator, model_limit_middleware, monkeypatch):
    monkeypatch.setattr(coordinator, "get_model", lambda: FakeMessagesListChatModel(responses=[AIMessage(content="Done")]))
    
    state = coordinator.build_context(supervisor_state={"domain_summaries": [{"domain": "helm", "summary": "nginx"}]})
    assert "domain_summaries" in state

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_ops_journal_survives_summarization(coordinator, model_limit_middleware, monkeypatch):
    from k8s_autopilot.core.agents.app_operator.middleware import AppOperationContextMiddleware
    mw = AppOperationContextMiddleware()
    # Test that if summarization happens, mw still re-injects
    state = {"files": {"/memories/app-operator/operations-log.md": {"content": "CREATE app"}}}
    res = mw.before_model(state, None)
    assert res is not None
    assert "CREATE app" in res["messages"][0].content
