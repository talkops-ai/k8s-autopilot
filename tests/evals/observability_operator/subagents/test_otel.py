"""
Eval: OpenTelemetry subagent tool selection with real LLM.

Tests that otel-operator correctly routes:
- Read-only queries → list/inspect tools
- Collector provisioning → otel_provision_collector
- Service onboarding → otel_annotate_deployment
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"

OTEL_WRITE_TOOLS = {
    "otel_provision_collector", "otel_patch_collector",
    "otel_patch_instrumentation", "otel_annotate_deployment",
    "otel_toggle_sampling_strategy", "otel_enable_spanmetrics_for_service",
}


@pytest.fixture(scope="module")
def otel_harness():
    return SubagentEvalHarness.for_opentelemetry()


# ─── Read-only scenarios ──────────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_list_collectors(otel_harness):
    """'[READ-ONLY] List collectors' → otel_list_collectors."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await otel_harness.run(
        "[READ-ONLY] List all OpenTelemetry Collector instances in the cluster"
    )

    assert "otel_list_collectors" in trace.tool_names or "read_mcp_resource" in trace.tool_names, (
        f"Expected otel_list_collectors, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & OTEL_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


@pytest.mark.timeout(90)
async def test_cardinality_detection(otel_harness):
    """'[READ-ONLY] Check for high cardinality' → otel_detect_cardinality."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await otel_harness.run(
        "[READ-ONLY] Detect high-cardinality attributes in the default collector"
    )

    assert "otel_detect_cardinality" in trace.tool_names, (
        f"Expected otel_detect_cardinality, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & OTEL_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


# ─── State-modifying scenarios ────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_onboard_service(otel_harness):
    """'[STATE-MODIFYING] Onboard checkout for auto-instrumentation' → otel_annotate_deployment."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await otel_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Onboard service checkout (Java, namespace default) "
        "for OpenTelemetry auto-instrumentation. Exporter endpoint: otel-collector:4317. "
        "Plan approved."
    )

    annotated = "otel_annotate_deployment" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert annotated or asked_human, (
        f"Expected otel_annotate_deployment or HITL, got: {trace.tool_names}"
    )
