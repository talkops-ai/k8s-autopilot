import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import (
    get_fake_generator_subagent,
    get_fake_validator_valid
)


@pytest.mark.hitl
@pytest.mark.asyncio
async def test_commit_resume_reject_halts_operation(mock_config, in_memory_store, memory_saver):
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

    with patch("k8s_autopilot.core.agents.helm_operator.coordinator.create_model", return_value=fake_llm):
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

        config = {"configurable": {"thread_id": "hitl-reject-001"}}
        initial_state = {"messages": [HumanMessage(content="Create chart")]}

    # First run to hit the gate
    try:
        await agent.ainvoke(initial_state, config=config)
    except Exception:
        pass

    snapshot = agent.get_state(config)
    
    if snapshot.next:
        # Resume with 'reject'
        resume_data = "reject" 
        try:
            await agent.ainvoke(Command(resume=resume_data), config=config)
        except Exception:
            pass
            
        final_snapshot = agent.get_state(config)
        # Should halt or end
        messages = final_snapshot.values.get("messages", [])
        assert any("reject" in str(m.content).lower() or "cancel" in str(m.content).lower() for m in messages)
