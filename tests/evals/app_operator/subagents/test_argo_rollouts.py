"""
Eval: Argo Rollouts subagent tool selection with real LLM.

Tests that argo-rollouts-onboarder correctly routes:
- Read-only queries → read_mcp_resource
- State-modifying queries → update/promote/migrate tools
- HITL triggers for state-modifying tools
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"

ROLLOUTS_WRITE_TOOLS = {
    "argo_delete_rollout", "argo_delete_experiment", "convert_deployment_to_rollout",
    "convert_rollout_to_deployment", "argo_manage_rollout_lifecycle",
    "argo_manage_legacy_deployment", "argo_create_rollout",
    "argo_configure_analysis_template", "create_stable_canary_services",
    "argo_update_rollout"
}


@pytest.fixture(scope="module")
def argo_rollouts_harness():
    return SubagentEvalHarness.for_argo_rollouts()


# ─── Read-only scenarios ──────────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_list_rollouts(argo_rollouts_harness):
    """'[READ-ONLY] List rollouts' → read_mcp_resource."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argo_rollouts_harness.run(
        "[READ-ONLY] List all Argo Rollouts in the cluster"
    )

    assert "read_mcp_resource" in trace.tool_names, (
        f"Expected read_mcp_resource, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & ROLLOUTS_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


# ─── State-modifying scenarios ────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_promote_rollout(argo_rollouts_harness):
    """'[STATE-MODIFYING] Promote rollout' → argo_manage_rollout_lifecycle."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argo_rollouts_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Promote the checkout rollout to the next canary step. Plan approved."
    )

    managed = "argo_manage_rollout_lifecycle" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert managed or asked_human, (
        f"Expected argo_manage_rollout_lifecycle or HITL, got: {trace.tool_names}"
    )


@pytest.mark.timeout(90)
async def test_migrate_deployment(argo_rollouts_harness):
    """'[STATE-MODIFYING] Migrate deployment' → convert_deployment_to_rollout."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argo_rollouts_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Migrate the checkout deployment to a rollout. "
        "Use canary strategy. Apply the changes. Plan approved."
    )

    converted = "convert_deployment_to_rollout" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert converted or asked_human, (
        f"Expected convert_deployment_to_rollout or HITL, got: {trace.tool_names}"
    )
