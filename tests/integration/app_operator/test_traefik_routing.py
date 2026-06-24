"""
Integration: Traefik edge routing — weighted canary, traffic split, NGINX migration.

Verifies that traffic-routing requests are correctly routed to traefik-edge-router
and that the coordinator does NOT cross-delegate to rollout or ArgoCD subagents.
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage

from tests.integration.app_operator.helpers import ExhaustingFakeModel, make_traefik_subagent, make_argocd_subagent, make_rollouts_subagent


def _tool_call_msg(name: str, call_id: str = "tc1", args: dict = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args or {}}],
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_traffic_split_routes_to_traefik(coordinator, monkeypatch):
    """
    'Split traffic 80/20' must route to traefik-edge-router, not rollouts.

    Intent translation table: "Split traffic / A/B test" → traefik-edge-router.
    This is a critical routing decision — wrong routing means wrong tool executes.
    """
    traefik_agent = make_traefik_subagent()
    rollouts_agent = make_rollouts_subagent()
    argocd_agent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("traefik-edge-router", "tc1", {
            "task": "[STATE-MODIFYING] Create weighted canary route: stable=80, canary=20 in namespace staging"
        }),
        _tool_call_msg("log_app_operation", "tc2", {"operation": "traffic_split"}),
        AIMessage(content="✅ Traffic split 80/20 configured."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa_traefik(): return [traefik_agent, rollouts_agent, argocd_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa_traefik)


    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Set traffic split to 80/20 for my frontend service")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "traefik-edge-router" in all_tool_calls, (
        f"Traffic split must go to traefik-edge-router. Got: {all_tool_calls}"
    )
    assert "argo-rollouts-onboarder" not in all_tool_calls, (
        "traefik traffic split must NOT go to argo-rollouts-onboarder"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_nginx_migration_routes_to_traefik(coordinator, monkeypatch):
    """
    'Migrate from NGINX to Traefik' must route to traefik-edge-router.
    """
    traefik_agent = make_traefik_subagent(response="Completed Traefik operation: NGINX migration complete")

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("write_todos", "tc1", {"todos": [{"title": "Migrate NGINX", "status": "pending"}]}),
        _tool_call_msg("traefik-edge-router", "tc2", {
            "task": "[STATE-MODIFYING] Migrate all NGINX ingresses in namespace staging to Traefik IngressRoutes"
        }),
        _tool_call_msg("log_app_operation", "tc3", {"operation": "nginx_migration"}),
        AIMessage(content="✅ NGINX to Traefik migration complete."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa0(): return [traefik_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa0)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Migrate all my NGINX ingresses to Traefik in staging namespace")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "traefik-edge-router" in all_tool_calls


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_traefik_read_only_no_log(coordinator, monkeypatch):
    """
    Read-only traefik queries (list routes) must NOT call log_app_operation.
    """
    traefik_agent = make_traefik_subagent(response="Routes: frontend-route (80/20), api-route (100/0)")

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("traefik-edge-router", "tc1", {
            "task": "[READ-ONLY] List all TraefikService routes in namespace staging"
        }),
        AIMessage(content="Found 2 routes."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa1(): return [traefik_agent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa1)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Show me all Traefik routes")]
    })

    all_tool_calls = [
        tc["name"]
        for msg in result["messages"]
        if hasattr(msg, "tool_calls") and msg.tool_calls
        for tc in msg.tool_calls
    ]
    assert "traefik-edge-router" in all_tool_calls
    assert "log_app_operation" not in all_tool_calls, "Read-only must not log"
