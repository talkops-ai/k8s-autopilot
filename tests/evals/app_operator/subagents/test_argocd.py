"""
Eval: ArgoCD subagent tool selection with real LLM.

Tests that argocd-onboarder correctly routes:
- Read-only queries → list/inspect tools or read_mcp_resource
- State-modifying queries → create/update/delete tools
- HITL triggers for state-modifying tools
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"

ARGOCD_WRITE_TOOLS = {
    "create_application", "update_application", "sync_application",
    "delete_application", "delete_project", "delete_repository",
    "onboard_repository_https", "onboard_repository_ssh", "create_project"
}


@pytest.fixture(scope="module")
def argocd_harness():
    return SubagentEvalHarness.for_argocd()


# ─── Read-only scenarios ──────────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_list_applications(argocd_harness):
    """'[READ-ONLY] List apps' → list_applications."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argocd_harness.run(
        "[READ-ONLY] List all ArgoCD applications in the cluster"
    )

    assert "list_applications" in trace.tool_names or "read_mcp_resource" in trace.tool_names, (
        f"Expected list_applications or read_mcp_resource, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & ARGOCD_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


@pytest.mark.timeout(90)
async def test_app_details(argocd_harness):
    """'[READ-ONLY] Get app details' → get_application_details."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argocd_harness.run(
        "[READ-ONLY] Show details for the checkout application"
    )

    assert "get_application_details" in trace.tool_names or "read_mcp_resource" in trace.tool_names, (
        f"Expected get_application_details, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & ARGOCD_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


# ─── State-modifying scenarios ────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_create_application(argocd_harness):
    """'[STATE-MODIFYING] Create app' → create_application."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argocd_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Create an application named checkout from repo https://github.com/foo. "
        "Path: /k8s. Namespace: default. Plan approved."
    )

    created = "create_application" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert created or asked_human, (
        f"Expected create_application or HITL, got: {trace.tool_names}"
    )


@pytest.mark.timeout(90)
async def test_sync_application(argocd_harness):
    """'[STATE-MODIFYING] Sync app' → sync_application."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await argocd_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Force sync the checkout application. Plan approved."
    )

    synced = "sync_application" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert synced or asked_human, (
        f"Expected sync_application or HITL, got: {trace.tool_names}"
    )
