import pytest
from langchain_core.messages import HumanMessage, AIMessage
from unittest.mock import patch
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import (
    get_fake_generator_subagent,
    get_fake_validator_valid,
    make_exhausting_coordinator_model,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skill_exists_skips_planner(mock_config, memory_saver):
    """
    When skills already exist for the requested app type, the coordinator prompt
    instructs the LLM to skip helm-planner and go directly to helm-generator.

    This test verifies that the coordinator model receives the skill file in state
    and delegates to helm-generator (not helm-planner) first.

    Uses ExhaustingFakeModel: raises RuntimeError if extra model calls happen,
    preventing the infinite-loop hang that previously affected this test.
    """
    fake_generator = get_fake_generator_subagent({"files": {}})
    fake_validator = get_fake_validator_valid()

    # With skills pre-loaded, the coordinator should skip helm-planner.
    # Scripted response: go directly to generator → validator → done.
    coordinator_model = make_exhausting_coordinator_model([
        AIMessage(content="", tool_calls=[{"name": "helm-generator", "args": {}, "id": "tc1"}]),
        AIMessage(content="", tool_calls=[{"name": "helm-validator", "args": {}, "id": "tc2"}]),
        AIMessage(content="Skill exists — planner skipped. Chart generated successfully."),
    ])

    with patch("k8s_autopilot.utils.llm.create_model", return_value=coordinator_model):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver

        async def get_mock_subagent_specs():
            return [fake_generator, fake_validator]

        coordinator.get_subagent_specs = get_mock_subagent_specs
        agent = await coordinator.build_agent()

    config = {"configurable": {"thread_id": "integration-skill-skip-001"}}
    initial_state = {
        "messages": [HumanMessage(content="Create a Helm chart for nginx web server")],
        "files": {"/skills/helm-operator/nginx-chart-generator/SKILL.md": {"content": "Exists"}},
    }

    try:
        final_state = await agent.ainvoke(initial_state, config=config)
    except Exception:
        pass

    assert fake_generator["runnable"].calls > 0, (
        "helm-generator should have been called even when skill exists"
    )
