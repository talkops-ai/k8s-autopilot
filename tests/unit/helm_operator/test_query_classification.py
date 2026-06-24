import pytest
from langchain_core.messages import HumanMessage, AIMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator

@pytest.mark.unit
@pytest.mark.asyncio
async def test_oos_query_classification(mock_config):
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self
    
    fake = BindableFakeModel(responses=[AIMessage(content="This is outside my scope. Please use the appropriate operator.")])
    
    with patch("k8s_autopilot.core.agents.helm_operator.coordinator.create_model", return_value=fake):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        agent = await coordinator.build_agent()
        config = {"configurable": {"thread_id": "test_oos"}}
        state = {"messages": [HumanMessage(content="give me argocd password")]}
        
        final_state = await agent.ainvoke(state, config=config)
        assert "outside my scope" in final_state["messages"][-1].content.lower()
        assert len(final_state.get("tool_history", [])) == 0

@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversational_query_classification(mock_config):
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self
    
    fake = BindableFakeModel(responses=[AIMessage(content="You're welcome! Let me know if you need anything else.")])
    
    with patch("k8s_autopilot.core.agents.helm_operator.coordinator.create_model", return_value=fake):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        agent = await coordinator.build_agent()
        config = {"configurable": {"thread_id": "test_conv"}}
        state = {"messages": [HumanMessage(content="thanks")]}
        
        final_state = await agent.ainvoke(state, config=config)
        assert "welcome" in final_state["messages"][-1].content.lower()
        assert len(final_state.get("tool_history", [])) == 0
