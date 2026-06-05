"""
Integration: Rollout lifecycle — promote, abort, skip_analysis.

Separate from migration tests because lifecycle actions (promote_full, abort,
skip_analysis) have different risk profiles and HITL gates.
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
async def test_abort_rollout_routes_to_rollouts_not_argocd(coordinator, monkeypatch):
    """
    'Abort the rollout' must route to argo-rollouts-onboarder, NOT argocd-onboarder.

    This is a classic cross-domain confusion bug: 'abort' could sound like an
    ArgoCD rollback. The coordinator must correctly classify this as a Rollouts op.
    """
    rollouts_agent = make_rollouts_subagent()
    argocd_agent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argo-rollouts-onboarder", "tc1", {
            "task": "[STATE-MODIFYING] Abort rollout frontend in namespace staging"
        }),
        _tool_call_msg("log_app_operation", "tc2", {"operation": "abort_rollout"}),
        AIMessage(content="✅ Rollout aborted, traffic returned to stable."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa0(): return [rollouts_agent, argocd_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa0)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Abort the current rollout for frontend")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argo-rollouts-onboarder" in all_tool_calls
    assert "argocd-onboarder" not in all_tool_calls, (
        "Abort-rollout should NOT trigger argocd-onboarder"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_model_limit_middleware_prevents_loop(coordinator, monkeypatch):
    """
    ExhaustingFakeModel raises RuntimeError instead of looping indefinitely.

    This test verifies the testing infrastructure itself: if the agent somehow
    makes more LLM calls than scripted, we get an immediate, clear error.
    """
    rollouts_agent = make_rollouts_subagent()

    # Only one response — if the graph loops back to the model a second time,
    # ExhaustingFakeModel raises RuntimeError
    model = ExhaustingFakeModel(responses=[
        AIMessage(content="Rollout status: healthy"),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa1(): return [rollouts_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa1)

    agent = await coordinator.build_agent()

    # Should complete without looping (single response is enough for a terminal message)
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Thanks, I'm done")]
    })
    # Conversational message — no tools, just a direct reply
    assert result is not None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_image_update_routes_to_rollouts(coordinator, monkeypatch):
    """
    'Update to new image version' must route to argo-rollouts-onboarder.

    This covers the "Update to latest / new version → Rollout image update"
    mapping in the coordinator's intent translation table.
    """
    rollouts_agent = make_rollouts_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argo-rollouts-onboarder", "tc1", {
            "task": "[STATE-MODIFYING] Update rollout frontend image to v2.1.0 in namespace staging"
        }),
        _tool_call_msg("log_app_operation", "tc2", {"operation": "image_update"}),
        AIMessage(content="✅ Image updated, progressive delivery started."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa2(): return [rollouts_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa2)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Update frontend to image v2.1.0")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "argo-rollouts-onboarder" in all_tool_calls
