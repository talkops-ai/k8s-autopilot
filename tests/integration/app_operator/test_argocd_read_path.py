"""
Integration: ArgoCD read-only path — list, status, logs.

Verifies the coordinator correctly delegates read-only queries to argocd-onboarder
and never calls log_app_operation (read-only invariant).
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage

from tests.integration.app_operator.helpers import ExhaustingFakeModel, make_argocd_subagent


def _tool_call_msg(name: str, call_id: str = "tc1", args: dict = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args or {}}],
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_list_apps_delegates_to_argocd(coordinator, monkeypatch):
    """
    'List all ArgoCD apps' must delegate to argocd-onboarder.
    This was the only implemented test before — preserved and upgraded to ExhaustingFakeModel.
    """
    mock_subagent = make_argocd_subagent(response="app1, app2, app3")

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argocd-onboarder", "tc1", {
            "task": "[READ-ONLY] List all ArgoCD applications"
        }),
        AIMessage(content="**🔍 ArgoCD Applications**\n\n| app1 | app2 | app3 |"),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa0(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa0)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="List all ArgoCD applications")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argocd-onboarder" in all_tool_calls


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_list_apps_does_not_call_log_operation(coordinator, monkeypatch):
    """
    Read-only list operation must NOT call log_app_operation.

    Critical invariant from coordinator system prompt:
    'Do NOT call log_app_operation' for read-only queries.
    """
    mock_subagent = make_argocd_subagent(response="app1, app2")

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argocd-onboarder", "tc1", {"task": "[READ-ONLY] List ArgoCD apps"}),
        AIMessage(content="Found 2 apps."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa1(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa1)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="List all apps")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "log_app_operation" not in all_tool_calls, (
        "Read-only 'list apps' must NOT call log_app_operation. "
        f"Tool calls found: {all_tool_calls}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_app_status_delegates_to_argocd(coordinator, monkeypatch):
    """
    'What is the status of my-app?' must delegate to argocd-onboarder (read-only).
    """
    mock_subagent = make_argocd_subagent(response="app: my-app, health: Healthy, sync: Synced")

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argocd-onboarder", "tc1", {
            "task": "[READ-ONLY] Get application details and sync status for my-app"
        }),
        AIMessage(content="**✅ my-app** — Healthy / Synced"),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa2(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa2)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="What's the health status of my-app?")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argocd-onboarder" in all_tool_calls
    # Still a read-only check
    assert "log_app_operation" not in all_tool_calls
