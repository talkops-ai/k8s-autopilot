import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import (
    get_fake_generator_subagent,
    get_fake_validator_valid
)


@pytest.mark.hitl
@pytest.mark.asyncio
async def test_commit_gate_interrupts_graph(mock_config, in_memory_store, memory_saver):
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self
    
    fake_llm = BindableFakeModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "helm-planner", "args": {}, "id": "tc0"}]),
        AIMessage(content="", tool_calls=[{"name": "helm-generator", "args": {}, "id": "tc1"}]),
        AIMessage(content="", tool_calls=[{"name": "helm-validator", "args": {}, "id": "tc2"}]),
        AIMessage(content="", tool_calls=[{"name": "request_user_input", "args": {"title": "Commit", "question_preview": "Push?", "options": ["push", "keep_local"]}, "id": "tc3"}]),
        AIMessage(content="Done"),
    ])

    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake_llm):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_store = lambda: in_memory_store
        coordinator.build_checkpointer = lambda: memory_saver
        
        from tests.integration.helm_operator.fixtures.mock_tools import get_fake_planner_subagent, MockSubAgent
        fake_planner = get_fake_planner_subagent()
        fake_generator = get_fake_generator_subagent(
            {"/workspace/helm-charts/nginx/Chart.yaml": "content"}
        )
        fake_validator = get_fake_validator_valid()

        fake_github_agent = MockSubAgent(name="github-agent", response_content="Pushed to GitHub")

        async def get_mock_subagent_specs():
            return [
                fake_planner,
                fake_generator, 
                fake_validator, 
                {"name": "github-agent", "description": "Commits to github", "runnable": fake_github_agent}
            ]

        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()

        config = {"configurable": {"thread_id": "hitl-gate-001"}}
        initial_state = {"messages": [HumanMessage(content="Create a new chart for nginx")]}

    try:
        await agent.ainvoke(initial_state, config=config)
    except Exception as e:
        # Check if it was an interrupt
        if "interrupt" not in type(e).__name__.lower() and "GraphInterrupt" not in str(type(e)):
            raise e

    snapshot = agent.get_state(config)
    assert snapshot.next, "Graph should be paused at a node (e.g., github-agent or hitl node)"
    
    # We could also verify that request_user_input tool was called if we tracked it
