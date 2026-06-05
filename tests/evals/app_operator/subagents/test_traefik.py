"""
Eval: Traefik subagent tool selection with real LLM.

Tests that traefik-edge-router correctly routes:
- Read-only queries → read_mcp_resource
- State-modifying queries → manage route tools
- HITL triggers for state-modifying tools
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"

TRAEFIK_WRITE_TOOLS = {
    "traefik_manage_weighted_routing", "traefik_manage_simple_route",
    "traefik_generate_routing_manifest"
}


@pytest.fixture(scope="module")
def traefik_harness():
    return SubagentEvalHarness.for_traefik()


# ─── Read-only scenarios ──────────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_list_routes(traefik_harness):
    """'[READ-ONLY] List routes' → read_mcp_resource."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await traefik_harness.run(
        "[READ-ONLY] List all Traefik routes in the cluster"
    )

    assert "read_mcp_resource" in trace.tool_names, (
        f"Expected read_mcp_resource, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & TRAEFIK_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


# ─── State-modifying scenarios ────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_manage_weighted_routing(traefik_harness):
    """'[STATE-MODIFYING] Weighted routing' → traefik_manage_weighted_routing."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await traefik_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Shift 10% of traffic to the checkout canary. "
        "Keep 90% on stable. Plan approved."
    )

    managed = "traefik_manage_weighted_routing" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert managed or asked_human, (
        f"Expected traefik_manage_weighted_routing or HITL, got: {trace.tool_names}"
    )


@pytest.mark.timeout(90)
async def test_manage_simple_route(traefik_harness):
    """'[STATE-MODIFYING] Simple route' → traefik_manage_simple_route."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await traefik_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Create a simple ingress route for checkout on hostname checkout.example.com. Plan approved."
    )

    managed = "traefik_manage_simple_route" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert managed or asked_human, (
        f"Expected traefik_manage_simple_route or HITL, got: {trace.tool_names}"
    )
