import pytest
from unittest.mock import patch
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langgraph.types import Command
from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.tools import tool
from k8s_autopilot.core.agents.helm_operator.middleware import build_helm_hitl_middleware

# Mock tools
@tool
def helm_install_chart(chart_name: str, release_name: str, namespace: str) -> str:
    """Mock helm install"""
    return "installed"

@tool
def helm_upgrade_release(release_name: str, chart_name: str, namespace: str) -> str:
    """Mock helm upgrade"""
    return "upgraded"

@tool
def helm_rollback_release(release_name: str, revision: str, namespace: str) -> str:
    """Mock helm rollback"""
    return "rolled back"

@tool
def helm_uninstall_release(release_name: str, namespace: str) -> str:
    """Mock helm uninstall"""
    return "uninstalled"

@tool
def kubectl_apply_manifest(manifest: str) -> str:
    """Mock kubectl apply"""
    return "applied"


class BindableFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs): return self


@pytest.mark.hitl
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name, tool_args, tool_func", [
    ("helm_install_chart", {"chart_name": "nginx", "release_name": "web", "namespace": "default"}, helm_install_chart),
    ("helm_upgrade_release", {"release_name": "web", "chart_name": "nginx", "namespace": "default"}, helm_upgrade_release),
    ("helm_rollback_release", {"release_name": "web", "revision": "1", "namespace": "default"}, helm_rollback_release),
    ("helm_uninstall_release", {"release_name": "web", "namespace": "default"}, helm_uninstall_release),
    # ("kubectl_apply_manifest", {"manifest": "kind: Pod"}, kubectl_apply_manifest)
])
async def test_destructive_tool_hitl(tool_name, tool_args, tool_func):
    from langchain.agents import create_agent
    
    fake_llm = BindableFakeModel(responses=[
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": tool_args, "id": "tc1"}]),
        AIMessage(content="Operation complete")
    ])

    agent = create_agent(
        model=fake_llm,
        tools=[tool_func],
        middleware=[build_helm_hitl_middleware()],
        checkpointer=InMemorySaver(),
        name="test-hitl-agent"
    )

    config = {"configurable": {"thread_id": f"hitl-{tool_name}"}}
    initial_state = {"messages": [HumanMessage(content=f"Execute {tool_name}")]}

    try:
        await agent.ainvoke(initial_state, config=config)
    except Exception as e:
        if "interrupt" not in type(e).__name__.lower() and "GraphInterrupt" not in str(type(e)):
            raise e

    snapshot = agent.get_state(config)
    assert snapshot.next, f"Graph should be paused at a node for {tool_name}"

    # Resume with approve
    await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)

    final_snapshot = agent.get_state(config)
    assert not final_snapshot.next, f"Graph should have completed after resume for {tool_name}"
