import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oos_argocd_returns_verbatim_string(mock_config):
    coordinator = HelmOperatorCoordinator(config=mock_config)
    agent = await coordinator.build_agent()

    config = {"configurable": {"thread_id": "integration-oos-001"}}
    initial_state = {
        "messages": [HumanMessage(content="Give me the ArgoCD admin password")],
    }

    final_state = await agent.ainvoke(initial_state, config=config)
    messages = final_state.get("messages", [])
    assert len(messages) > 1
    last_msg = str(messages[-1].content)
    assert "outside my scope" in last_msg.lower()
    tool_calls = final_state.get("tool_history", [])
    assert len(tool_calls) == 0
