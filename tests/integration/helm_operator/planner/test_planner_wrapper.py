import pytest
from unittest.mock import AsyncMock, patch
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator

@pytest.fixture
def mock_planner_graph():
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [], "files": {}}
    return graph

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_wrapper_extracts_session_id_from_config(mock_config, mock_planner_graph):
    with patch("k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent.HelmPlannerSupervisorAgent.build_graph", return_value=mock_planner_graph):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        specs = await coordinator.get_subagent_specs()
        planner_wrapper = next(s for s in specs if s["name"] == "helm-planner")["runnable"]

        config = {"configurable": {"context": {"session_id": "sess-x"}, "thread_id": "123"}}
        state = {"messages": [HumanMessage(content="plan chart")]}

        await planner_wrapper.ainvoke(state, config=config)
        call_args = mock_planner_graph.ainvoke.call_args[0]
        subgraph_input = call_args[0]
        assert subgraph_input["session_id"] == "sess-x"
        assert subgraph_input["user_query"] == "plan chart"

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_wrapper_falls_back_to_human_message(mock_config, mock_planner_graph):
    with patch("k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent.HelmPlannerSupervisorAgent.build_graph", return_value=mock_planner_graph):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        specs = await coordinator.get_subagent_specs()
        planner_wrapper = next(s for s in specs if s["name"] == "helm-planner")["runnable"]

        config = {"configurable": {"thread_id": "123"}}
        state = {"messages": [HumanMessage(content="plan this chart")]}

        await planner_wrapper.ainvoke(state, config=config)
        call_args = mock_planner_graph.ainvoke.call_args[0]
        subgraph_input = call_args[0]
        assert subgraph_input["user_query"] == "plan this chart"

@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_wrapper_merges_parent_files(mock_config):
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [], "files": {"/skills/new.md": {"content": "new"}}}
    
    with patch("k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent.HelmPlannerSupervisorAgent.build_graph", return_value=graph):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        specs = await coordinator.get_subagent_specs()
        planner_wrapper = next(s for s in specs if s["name"] == "helm-planner")["runnable"]

        config = {"configurable": {"thread_id": "123"}}
        state = {"messages": [HumanMessage(content="plan this chart")], "files": {"/skills/parent.md": {"content": "parent"}}}

        result = await planner_wrapper.ainvoke(state, config=config)
        assert "/skills/new.md" in result.get("files", {})
        assert "/skills/parent.md" in result.get("files", {})
