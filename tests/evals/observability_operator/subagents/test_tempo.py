"""
Eval: Tempo subagent tool selection with real LLM.

Tests that tempo-operator correctly routes:
- Trace search → tempo_traceql_search
- Trace summarization → tempo_summarize_trace
- CRD creation → tempo_create_operator_cr
- Read-only queries don't trigger CRD tools
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"

TEMPO_WRITE_TOOLS = {"tempo_create_operator_cr", "tempo_patch_operator_cr"}


@pytest.fixture(scope="module")
def tempo_harness():
    return SubagentEvalHarness.for_tempo()


# ─── Read-only scenarios ──────────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_trace_search(tempo_harness):
    """'[READ-ONLY] Find slow checkout requests' → tempo_traceql_search."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await tempo_harness.run(
        "[READ-ONLY] Find traces for service checkout with duration above 2s in the last hour"
    )

    assert "tempo_traceql_search" in trace.tool_names, (
        f"Expected tempo_traceql_search, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & TEMPO_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


@pytest.mark.timeout(90)
async def test_trace_summarize(tempo_harness):
    """'[READ-ONLY] Summarize trace abc123' → tempo_summarize_trace."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await tempo_harness.run(
        "[READ-ONLY] Summarize trace ID abc123 — show critical path and errors"
    )

    # Should call get_trace or summarize_trace
    valid = {"tempo_summarize_trace", "tempo_get_trace"}
    called = set(trace.tool_names) & valid
    assert called, (
        f"Expected tempo_summarize_trace or tempo_get_trace, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & TEMPO_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


@pytest.mark.timeout(90)
async def test_service_topology(tempo_harness):
    """'[READ-ONLY] Show service dependencies' → tempo_get_service_dependencies."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await tempo_harness.run(
        "[READ-ONLY] Show the service dependency topology"
    )

    assert "tempo_get_service_dependencies" in trace.tool_names, (
        f"Expected tempo_get_service_dependencies, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & TEMPO_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


# ─── State-modifying scenarios ────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_create_tempostack_cr(tempo_harness):
    """'[STATE-MODIFYING] Create a TempoStack' → tempo_create_operator_cr."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await tempo_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Create a TempoStack CR named 'prod' "
        "in namespace tempo-system with S3 storage backend (bucket: tempo-traces, "
        "endpoint: s3.amazonaws.com). Retention: 720h. Enable Jaeger UI. Plan approved."
    )

    created = "tempo_create_operator_cr" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert created or asked_human, (
        f"Expected tempo_create_operator_cr or HITL, got: {trace.tool_names}\n"
        f"Final: {trace.final_message[:200]}"
    )
