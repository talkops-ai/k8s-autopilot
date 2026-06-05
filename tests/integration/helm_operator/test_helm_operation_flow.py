import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import MockSubAgent

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_releases_calls_helm_operation(mock_config, memory_saver, in_memory_store):
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self

    fake = BindableFakeModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "helm-operation", "args": {}, "id": "tc1"}]),
        AIMessage(content="Done", tool_calls=[])
    ])

    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake):
        fake_agent = MockSubAgent(name="helm-operation", response_content="List of releases")
        fake_operation = {"name": "helm-operation", "description": "mock", "runnable": fake_agent}
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver
        coordinator._store = in_memory_store

        async def get_mock_subagent_specs():
            return [fake_operation]
        
        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()

        config = {"configurable": {"thread_id": "integration-helm-operation-001"}}
        initial_state = {
            "messages": [HumanMessage(content="List all Helm releases")],
        }

        try:
            await agent.ainvoke(initial_state, config=config)
        except Exception:
            pass
        
        snapshot = agent.get_state(config)
        messages = snapshot.values.get("messages", [])
        assert len(messages) > 1
        assert fake_operation["runnable"].calls > 0
