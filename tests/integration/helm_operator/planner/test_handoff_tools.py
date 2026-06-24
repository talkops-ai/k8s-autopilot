import pytest
from k8s_autopilot.core.agents.helm_operator.helm_planner.planner_supervisor_agent import HelmPlannerSupervisorAgent

@pytest.mark.integration
@pytest.mark.asyncio
async def test_handoff_tools_exist(mock_config):
    planner = HelmPlannerSupervisorAgent(config=mock_config)
    planner._initialize_sub_agents()
    
    assert planner._req_analyser_agent is not None
    assert planner._arch_planner_agent is not None
