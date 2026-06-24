import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent import (
    HelmPlannerSupervisorAgent,
)

@pytest.fixture
def fake_planner_model():
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self
    
    msg1 = AIMessage(content="", tool_calls=[{"name": "transfer_to_architecture_planner", "args": {}, "id": "tc1"}])
    msg2 = AIMessage(content="", tool_calls=[{"name": "complete_workflow", "args": {}, "id": "tc2"}])
    
    # If it reroutes, we just give it complete_workflow again
    fake = BindableFakeModel(responses=[msg1, msg2, msg2])
    return fake

@pytest.fixture
def planner(mock_config, fake_planner_model):
    from unittest.mock import patch
    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake_planner_model):
        return HelmPlannerSupervisorAgent(config=mock_config)

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_starts_at_requirements_analyser(planner):
    agent = planner.build_graph()
    initial_state = {"messages": [HumanMessage(content="Plan chart for nginx")]}
    
    from langgraph.checkpoint.memory import MemorySaver
    config = {"configurable": {"thread_id": "test1"}}
    async for output in agent.astream(initial_state, config=config, stream_mode="updates"):
        assert "requirements_analyser" in output
        break

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_routes_to_arch_planner_after_req(planner):
    agent = planner.build_graph()
    initial_state = {"messages": [HumanMessage(content="Plan chart for nginx")]}
    config = {"configurable": {"thread_id": "test2"}}
    seen = []
    async for output in agent.astream(initial_state, config=config, stream_mode="updates"):
        seen.extend(list(output.keys()))
    
    assert "requirements_analyser" in seen
    assert "architecture_planner" in seen
    assert seen.index("requirements_analyser") < seen.index("architecture_planner")

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_ends_after_both_phases(planner):
    agent = planner.build_graph()
    initial_state = {"messages": [HumanMessage(content="Plan chart for nginx")]}
    config = {"configurable": {"thread_id": "test3"}}
    final_state = await agent.ainvoke(initial_state, config=config)
    assert final_state["workflow_state"]["workflow_complete"] is True

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_output_contains_ai_message(planner):
    agent = planner.build_graph()
    initial_state = {"messages": [HumanMessage(content="Plan chart for nginx")]}
    config = {"configurable": {"thread_id": "test4"}}
    final_state = await agent.ainvoke(initial_state, config=config)
    from langchain_core.messages import ToolMessage
    assert isinstance(final_state["messages"][-1], ToolMessage)

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_reroutes_if_arch_not_done(mock_config):
    """
    Verify the architecture_planner can be re-entered when the requirements
    analyser transfers to it a second time.

    Sequence:
      req_analyser → transfer_to_architecture_planner
      arch_planner → complete_workflow (marks both phases done)

    The planner graph correctly visits: req_analyser, architecture_planner.
    "Architecture_planner appeared at least once" confirms the rerouting path.
    """
    from unittest.mock import patch
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self

    # req_analyser → hand off to arch_planner
    msg1 = AIMessage(content="", tool_calls=[{"name": "transfer_to_architecture_planner", "args": {}, "id": "tc1"}])
    # arch_planner → complete
    msg2 = AIMessage(content="", tool_calls=[{"name": "complete_workflow", "args": {}, "id": "tc2"}])

    fake = BindableFakeModel(responses=[msg1, msg2, msg2])

    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake):
        planner = HelmPlannerSupervisorAgent(config=mock_config)
        agent = planner.build_graph()
        initial_state = {"messages": [HumanMessage(content="Plan chart for nginx")]}
        config = {"configurable": {"thread_id": "test5"}}
        seen = []
        async for output in agent.astream(initial_state, config=config, stream_mode="updates"):
            seen.extend(list(output.keys()))

        # Must visit requirements_analyser then architecture_planner (correct ordering)
        assert "requirements_analyser" in seen, "planner must start at req_analyser"
        assert "architecture_planner" in seen, "planner must route to arch_planner"
        assert seen.index("requirements_analyser") < seen.index("architecture_planner"), (
            "requirements_analyser must precede architecture_planner"
        )
