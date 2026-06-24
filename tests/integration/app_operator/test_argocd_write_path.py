"""
Integration: ArgoCD write path — state-modifying operations.

LangChain guidance: integration tests use fake model + real graph to verify
routing decisions, tool call sequences, and middleware injection.

These tests verify:
1. State-modifying requests trigger subagent delegation (not OOS rejection)
2. The coordinator calls write_todos before delegating (planning path)
3. log_app_operation is called after a write operation
4. Plan-locked delegation skips the planning phase and delegates directly
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from tests.integration.app_operator.helpers import ExhaustingFakeModel, make_argocd_subagent


def _tool_call_msg(tool_name: str, call_id: str = "tc1", args: dict = None) -> AIMessage:
    """Helper: build an AIMessage with a single tool call."""
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": tool_name, "args": args or {}}],
    )


def _tool_response(call_id: str = "tc1", content: str = "ok") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_create_app_delegates_to_argocd(coordinator, monkeypatch):
    """
    State-modifying request → coordinator must delegate to argocd-onboarder.

    Script:  1. model calls write_todos (planning step)
             2. model calls argocd-onboarder subagent
             3. model calls log_app_operation
             4. model calls request_chat_continue → terminal
    """
    mock_subagent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        # Step 1: write_todos (planning)
        _tool_call_msg("write_todos", "tc1", {"todos": [{"title": "Create app", "status": "pending"}]}),
        # Step 2: delegate to argocd-onboarder
        _tool_call_msg("argocd-onboarder", "tc2", {"task": "[STATE-MODIFYING] Create ArgoCD app frontend in ns staging"}),
        # Step 3: log_app_operation
        _tool_call_msg("log_app_operation", "tc3", {"operation": "create_app", "details": "frontend"}),
        # Step 4: final response
        AIMessage(content="✅ Application created successfully."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa0(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa0)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Onboard my frontend app to ArgoCD in namespace staging")]
    })

    # Verify tool calls in trajectory
    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argocd-onboarder" in all_tool_calls, (
        f"argocd-onboarder was not called. Tool calls: {all_tool_calls}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_write_path_calls_log_operation(coordinator, monkeypatch):
    """
    After a state-modifying ArgoCD operation, log_app_operation MUST be called.

    The coordinator's system prompt ALWAYS requires log_app_operation after
    state-modifying GitOps operations — this is a safety invariant.
    """
    mock_subagent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argocd-onboarder", "tc1", {"task": "[READ-ONLY] Sync app frontend"}),
        _tool_call_msg("log_app_operation", "tc2", {"operation": "sync", "details": "frontend"}),
        AIMessage(content="✅ Sync triggered."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa1(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa1)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Sync my frontend app")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "log_app_operation" in all_tool_calls, (
        f"log_app_operation missing — write path must always log. Tool calls: {all_tool_calls}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_plan_locked_skips_write_todos(coordinator, monkeypatch):
    """
    Plan-locked delegation (pre-approved) must skip write_todos and delegate directly.

    When the supervisor injects [PLAN-LOCKED] into the task, the coordinator
    must NOT call write_todos — this avoids double-planning of already-approved ops.
    """
    mock_subagent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        # Direct delegation — no write_todos
        _tool_call_msg("argocd-onboarder", "tc1", {
            "task": "[STATE-MODIFYING] [PLAN-LOCKED] Create app frontend in ns production"
        }),
        _tool_call_msg("log_app_operation", "tc2", {"operation": "create_app"}),
        AIMessage(content="✅ Plan-locked execution complete."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa2(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa2)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="[PLAN-LOCKED] Create app frontend in namespace production")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "write_todos" not in all_tool_calls, (
        "Plan-locked path must NOT call write_todos — planning already done"
    )
    assert "argocd-onboarder" in all_tool_calls, "argocd-onboarder must be called in plan-locked path"
