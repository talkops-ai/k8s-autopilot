import os
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from k8s_autopilot.config.config import Config
from k8s_autopilot.core.agents.app_operator.coordinator import AppOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import MockSubAgent

@pytest.fixture(scope="module")
def real_config():
    return Config({"MCP_SERVERS": []})

@pytest.fixture
def memory_saver():
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()

async def _build_coordinator_with_mocked_subagents(real_config, memory_saver, subagent_specs):
    coordinator = AppOperatorCoordinator(config=real_config)
    coordinator.build_checkpointer = lambda: memory_saver
    
    async def get_mock_subagent_specs():
        return subagent_specs

    coordinator.get_subagent_specs = get_mock_subagent_specs
    agent = await coordinator.build_agent()
    return agent

def _extract_trace(result):
    messages = result.get("messages", [])
    tool_calls_all = []
    final_message = ""

    for msg in messages:
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, list):
                # Gemini thinking-mode: filter out 'thinking' blocks, keep 'text' only
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text"):
                            text_parts.append(part["text"])
                        elif "text" in part and part.get("type") != "thinking":
                            text_parts.append(str(part["text"]))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = " ".join(text_parts).strip()
            final_message = str(content or "")
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    tool_calls_all.append((name, tc.get("args", {})))
                    
    return {
        "tool_calls": tool_calls_all,
        "final_message": final_message,
    }



def get_mock_app_subagents():
    return [
        {"name": "argocd-onboarder", "description": "argocd", "runnable": MockSubAgent("argocd-onboarder", "Done")},
        {"name": "argo-rollouts-onboarder", "description": "rollouts", "runnable": MockSubAgent("argo-rollouts-onboarder", "Done")},
        {"name": "traefik-edge-router", "description": "traefik", "runnable": MockSubAgent("traefik-edge-router", "Done")},
    ]


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_oos_helm_classification(real_config, memory_saver):
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")
        
    agent = await _build_coordinator_with_mocked_subagents(real_config, memory_saver, get_mock_app_subagents())
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Create a Helm chart for my application.")]
    }, config={"configurable": {"thread_id": "eval-oos-helm"}})
    
    trace = _extract_trace(result)
    assert "outside my scope" in trace["final_message"].lower()
    assert len(trace["tool_calls"]) == 0

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_oos_k8s_pods_classification(real_config, memory_saver):
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")
        
    agent = await _build_coordinator_with_mocked_subagents(real_config, memory_saver, get_mock_app_subagents())
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Show me Kubernetes pods")]
    }, config={"configurable": {"thread_id": "eval-oos-pods"}})
    
    trace = _extract_trace(result)
    assert "outside my scope" in trace["final_message"].lower()
    assert len(trace["tool_calls"]) == 0

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_conversational_classification(real_config, memory_saver):
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")
        
    agent = await _build_coordinator_with_mocked_subagents(real_config, memory_saver, get_mock_app_subagents())
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Thanks, I'm done!")]
    }, config={"configurable": {"thread_id": "eval-conv"}})
    
    trace = _extract_trace(result)
    assert trace["final_message"].strip() != ""
    assert len(trace["tool_calls"]) == 0

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_read_only_classification(real_config, memory_saver):
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")
        
    agent = await _build_coordinator_with_mocked_subagents(real_config, memory_saver, get_mock_app_subagents())
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="List my ArgoCD apps")]
    }, config={"configurable": {"thread_id": "eval-ro"}})
    
    trace = _extract_trace(result)
    task_calls = [tc for tc in trace["tool_calls"] if tc[0] == "task"]
    chat_calls = [tc for tc in trace["tool_calls"] if tc[0] == "request_chat_continue"]
    
    # Must NOT write_todos or [STATE-MODIFYING]
    todos_calls = [tc for tc in trace["tool_calls"] if tc[0] == "write_todos"]
    assert len(todos_calls) == 0, "Read-only query should not trigger a plan"
    
    if len(task_calls) > 0:
        argocd_calls = [tc for tc in task_calls if tc[1].get("subagent_name") == "argocd-onboarder"]
        if argocd_calls:
            instruction = argocd_calls[0][1].get("instruction", "")
            assert "[READ-ONLY]" in instruction
    else:
        assert len(chat_calls) > 0, "If no task was delegated, it must have responded via chat_continue"

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_state_modifying_triggers_plan(real_config, memory_saver):
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")
        
    agent = await _build_coordinator_with_mocked_subagents(real_config, memory_saver, get_mock_app_subagents())
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Set up canary deployment for web in default namespace")]
    }, config={"configurable": {"thread_id": "eval-state"}})
    
    trace = _extract_trace(result)
    task_calls = [tc for tc in trace["tool_calls"] if tc[0] == "task"]
    todos_calls = [tc for tc in trace["tool_calls"] if tc[0] == "write_todos"]
    
    # It must EITHER write todos (plan) OR immediately delegate with [STATE-MODIFYING]
    if not todos_calls:
        assert len(task_calls) > 0, "Must write_todos or delegate via task"
        instruction = task_calls[0][1].get("instruction", "")
        assert "[STATE-MODIFYING]" in instruction

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_ambiguous_intent_asks_user(real_config, memory_saver):
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")
        
    agent = await _build_coordinator_with_mocked_subagents(real_config, memory_saver, get_mock_app_subagents())
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Scale it")]
    }, config={"configurable": {"thread_id": "eval-ambiguous"}})
    
    trace = _extract_trace(result)
    
    # Should use chat continue to ask, rather than delegating blindly
    chat_calls = [tc for tc in trace["tool_calls"] if tc[0] == "request_chat_continue"]
    task_calls = [tc for tc in trace["tool_calls"] if tc[0] == "task"]
    
    # Ideally no task calls for completely ambiguous queries without params
    if len(task_calls) > 0:
        instruction = task_calls[0][1].get("instruction", "")
        assert "[READ-ONLY]" in instruction, "Must only do read-only discovery, not state modifying"
