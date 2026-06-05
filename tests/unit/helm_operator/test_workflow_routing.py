import pytest
from langchain_core.messages import HumanMessage, AIMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from langchain_core.runnables import RunnableLambda

@pytest.mark.unit
@pytest.mark.asyncio
async def test_workflow_routing_skill_exists_skips_planner(mock_config):
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self
    
    fake = BindableFakeModel(responses=[AIMessage(content="", tool_calls=[{"name": "helm-generator", "args": {}, "id": "tc1"}])])
    
    with patch("k8s_autopilot.core.agents.helm_operator.coordinator.create_model", return_value=fake):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        async def get_mock_subagent_specs():
            return [{"name": "helm-generator", "description": "mock", "runnable": RunnableLambda(lambda x: x)}]
        coordinator.get_subagent_specs = get_mock_subagent_specs
        
        agent = await coordinator.build_agent()
        config = {"configurable": {"thread_id": "test_routing_1"}}
        state = {
            "messages": [HumanMessage(content="Create chart")],
            "files": {"/skills/nginx/SKILL.md": {"content": "skill"}}
        }
        final_state = await agent.ainvoke(state, config=config)
        subagents_called = final_state.get("subagents_called", [])
        assert "helm-planner" not in subagents_called
