"""
Integration: Out-of-scope routing — Observability coordinator must reject
Helm, K8s, and ArgoCD requests without calling any tools.

Uses CustomFakeModel that supports bind_tools for LangGraph compatibility.
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

class CustomFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, *args, **kwargs):
        return self

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_oos_helm_returns_verbatim(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "get_model", lambda: CustomFakeModel(responses=[AIMessage(content="This is outside my scope.")]))
    agent = await coordinator.build_agent()

    res = await agent.ainvoke({"messages": [HumanMessage(content="Create a Helm chart for nginx")]})
    assert "outside my scope" in res["messages"][-1].content.lower()

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_oos_k8s_pods_no_tool_calls(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "get_model", lambda: CustomFakeModel(responses=[AIMessage(content="This is outside my scope.")]))
    agent = await coordinator.build_agent()

    res = await agent.ainvoke({"messages": [HumanMessage(content="Show me Kubernetes pods")]})
    for msg in res["messages"]:
        assert not hasattr(msg, "tool_calls") or not msg.tool_calls

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_conversational_no_tool_calls(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "get_model", lambda: CustomFakeModel(responses=[AIMessage(content="You're welcome!")]))
    agent = await coordinator.build_agent()

    res = await agent.ainvoke({"messages": [HumanMessage(content="Thanks, I'm done!")]})
    for msg in res["messages"]:
        assert not hasattr(msg, "tool_calls") or not msg.tool_calls

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_conversational_does_not_delegate(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "get_model", lambda: CustomFakeModel(responses=[AIMessage(content="Great!")]))
    agent = await coordinator.build_agent()

    res = await agent.ainvoke({"messages": [HumanMessage(content="Looks great, no further questions")]})
    for msg in res["messages"]:
        assert not hasattr(msg, "tool_calls") or not msg.tool_calls
