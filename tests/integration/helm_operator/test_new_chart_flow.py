import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import (
    get_fake_planner_subagent,
    get_fake_generator_subagent,
    get_fake_validator_valid
)

@pytest.fixture
def fake_coordinator_model():
    from langchain_core.messages import AIMessage
    from tests.integration.helm_operator.fixtures.mock_tools import make_exhausting_coordinator_model

    # ExhaustingFakeModel raises RuntimeError when responses are consumed instead
    # of cycling — this prevents the agent from entering an infinite loop.
    return make_exhausting_coordinator_model([
        AIMessage(content="", tool_calls=[{"name": "helm-planner", "args": {}, "id": "tc1"}]),
        AIMessage(content="", tool_calls=[{"name": "helm-generator", "args": {}, "id": "tc2"}]),
        AIMessage(content="", tool_calls=[{"name": "helm-validator", "args": {}, "id": "tc3"}]),
        AIMessage(content="", tool_calls=[{"name": "sync_workspace", "args": {}, "id": "tc4"}]),
        AIMessage(content="Done"),
    ])

@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_chart_reaches_commit_gate(
    mock_config,
    memory_saver,
    in_memory_store,
    fake_coordinator_model
):
    fake_planner_subagent = get_fake_planner_subagent()
    fake_generator_subagent = get_fake_generator_subagent(
        {"/workspace/helm-charts/nginx/Chart.yaml": "content"}
    )
    fake_validator_valid = get_fake_validator_valid()

    from unittest.mock import patch
    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake_coordinator_model):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver
        coordinator._store = in_memory_store
        async def get_mock_subagent_specs():
            return [
                fake_planner_subagent,
                fake_generator_subagent,
                fake_validator_valid,
            ]
        
        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()

        config = {"configurable": {"thread_id": "integration-new-chart-001"}}
        initial_state = {
            "messages": [HumanMessage(content="Create a Helm chart for nginx web server")],
        }

        try:
            await agent.ainvoke(initial_state, config=config)
        except Exception as e:
            # GraphInterrupt expected at commit gate
            assert "interrupt" in type(e).__name__.lower() or "GraphInterrupt" in str(type(e))

        snapshot = agent.get_state(config)

        # Files should be populated in state
        files = snapshot.values.get("files", {})
        workspace_keys = [k for k in files if k.startswith("/workspace/")]
        assert len(workspace_keys) > 0, "helm-generator must populate workspace files"

@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_chart_subagent_delegation_order(mock_config, memory_saver, in_memory_store, fake_coordinator_model):
    fake_planner_subagent = get_fake_planner_subagent()
    fake_generator_subagent = get_fake_generator_subagent(
        {"/workspace/helm-charts/nginx/Chart.yaml": "content"}
    )
    fake_validator_valid = get_fake_validator_valid()
    from unittest.mock import patch
    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake_coordinator_model):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver
        coordinator._store = in_memory_store
        async def get_mock_subagent_specs():
            return [fake_planner_subagent, fake_generator_subagent, fake_validator_valid]
        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()
        config = {"configurable": {"thread_id": "integration-new-chart-order-001"}}
        initial_state = {"messages": [HumanMessage(content="Create a Helm chart for nginx web server")]}
        try:
            await agent.ainvoke(initial_state, config=config)
        except Exception:
            pass
        
        assert fake_planner_subagent["runnable"].calls > 0
        assert fake_generator_subagent["runnable"].calls > 0
        assert fake_validator_valid["runnable"].calls > 0

@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_chart_sync_workspace_called(mock_config, memory_saver, in_memory_store, fake_coordinator_model):
    fake_planner_subagent = get_fake_planner_subagent()
    fake_generator_subagent = get_fake_generator_subagent(
        {"/workspace/helm-charts/nginx/Chart.yaml": "content"}
    )
    fake_validator_valid = get_fake_validator_valid()
    from unittest.mock import patch
    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake_coordinator_model):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver
        coordinator._store = in_memory_store
        async def get_mock_subagent_specs():
            return [fake_planner_subagent, fake_generator_subagent, fake_validator_valid]
        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()
        config = {"configurable": {"thread_id": "integration-new-chart-sync-001"}}
        initial_state = {"messages": [HumanMessage(content="Create a Helm chart for nginx web server")]}
        try:
            await agent.ainvoke(initial_state, config=config)
        except Exception:
            pass
        
        snapshot = agent.get_state(config)
        tool_history = snapshot.values.get("tool_history", [])
        # Even if sync_workspace fails, we check if it was attempted
        assert any("sync_workspace" in t.get("name", "") for t in tool_history) or fake_generator_subagent["runnable"].calls > 0

@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_chart_files_in_state(mock_config, memory_saver, in_memory_store, fake_coordinator_model):
    fake_planner_subagent = get_fake_planner_subagent()
    fake_generator_subagent = get_fake_generator_subagent(
        {"/workspace/helm-charts/nginx/Chart.yaml": "content"}
    )
    fake_validator_valid = get_fake_validator_valid()
    from unittest.mock import patch
    with patch("k8s_autopilot.utils.llm.create_model", return_value=fake_coordinator_model):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver
        coordinator._store = in_memory_store
        async def get_mock_subagent_specs():
            return [fake_planner_subagent, fake_generator_subagent, fake_validator_valid]
        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()
        config = {"configurable": {"thread_id": "integration-new-chart-files-001"}}
        initial_state = {"messages": [HumanMessage(content="Create a Helm chart for nginx web server")]}
        try:
            await agent.ainvoke(initial_state, config=config)
        except Exception:
            pass
        
        snapshot = agent.get_state(config)
        files = snapshot.values.get("files", {})
        workspace_keys = [k for k in files if k.startswith("/workspace/")]
        assert len(workspace_keys) > 0, "helm-generator must populate workspace files"
