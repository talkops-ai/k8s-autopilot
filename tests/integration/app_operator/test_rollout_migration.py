"""
Integration: Argo Rollouts — migration and lifecycle operations.

Verifies the coordinator correctly routes rollout requests to
argo-rollouts-onboarder and does NOT cross-delegate to argocd or traefik.
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage

from tests.integration.app_operator.helpers import ExhaustingFakeModel, make_rollouts_subagent, make_argocd_subagent


def _tool_call_msg(name: str, call_id: str = "tc1", args: dict = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args or {}}],
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_migration_request_routes_to_rollouts(coordinator, monkeypatch):
    """
    'Migrate my deployment to canary rollout' must route to argo-rollouts-onboarder only.

    LangChain trajectory principle: verify the agent called the right subagent
    and did NOT accidentally delegate to a wrong one (cross-domain routing bug).
    """
    rollouts_agent = make_rollouts_subagent()
    argocd_agent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("write_todos", "tc1", {"todos": [{"title": "Migrate frontend", "status": "pending"}]}),
        _tool_call_msg("argo-rollouts-onboarder", "tc2", {
            "task": "[STATE-MODIFYING] Convert deployment frontend to canary Rollout in namespace staging"
        }),
        _tool_call_msg("log_app_operation", "tc3", {"operation": "migration"}),
        AIMessage(content="✅ Migration complete."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa0(): return [rollouts_agent, argocd_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa0)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Migrate my frontend deployment to a canary rollout in namespace staging")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    # Correct routing
    assert "argo-rollouts-onboarder" in all_tool_calls, (
        f"Rollout migration must go to argo-rollouts-onboarder. Got: {all_tool_calls}"
    )
    # No cross-domain leakage
    assert "argocd-onboarder" not in all_tool_calls, (
        "argocd-onboarder must NOT be called for a rollout migration"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_promote_full_requires_log_operation(coordinator, monkeypatch):
    """
    'Promote canary to 100%' (promote_full) is a state-modifying op — log_app_operation required.
    """
    rollouts_agent = make_rollouts_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argo-rollouts-onboarder", "tc1", {
            "task": "[STATE-MODIFYING] promote_full rollout frontend in ns staging"
        }),
        _tool_call_msg("log_app_operation", "tc2", {"operation": "promote_full"}),
        AIMessage(content="✅ Full promotion complete."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa1(): return [rollouts_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa1)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Promote the frontend canary to 100%")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argo-rollouts-onboarder" in all_tool_calls
    assert "log_app_operation" in all_tool_calls, (
        "promote_full is a state-modifying op — log_app_operation is mandatory"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_read_only_rollout_status_no_log(coordinator, monkeypatch):
    """
    Read-only rollout status queries must NOT call log_app_operation.

    The coordinator's rules: 'Do NOT call log_app_operation' for read-only queries.
    """
    rollouts_agent = make_rollouts_subagent(response="Rollout status: healthy, step 2/5")

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argo-rollouts-onboarder", "tc1", {
            "task": "[READ-ONLY] Get rollout status for frontend in ns staging"
        }),
        AIMessage(content="✅ Rollout is healthy."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa2(): return [rollouts_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa2)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="What is the rollout status for frontend?")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argo-rollouts-onboarder" in all_tool_calls
    assert "log_app_operation" not in all_tool_calls, (
        "Read-only query must NOT call log_app_operation"
    )
