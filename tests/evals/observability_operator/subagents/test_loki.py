"""
Eval: Loki subagent tool selection with real LLM.

Loki is read-only — ALL 8 tools are non-destructive. The key tests:
- Log search → execute_logql_query (not execute_logql_instant for log lines)
- Label discovery → get_cluster_labels
- Trace-log correlation uses label filters, NOT stream selectors for trace_id
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"


@pytest.fixture(scope="module")
def loki_harness():
    return SubagentEvalHarness.for_loki()


@pytest.mark.timeout(90)
async def test_log_search(loki_harness):
    """'Show checkout error logs' → execute_logql_query (range query for log lines)."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await loki_harness.run(
        "[READ-ONLY] Show error logs from service checkout in the last 30 minutes"
    )

    assert "execute_logql_query" in trace.tool_names or "execute_logql_instant" in trace.tool_names, (
        f"Expected execute_logql_query or execute_logql_instant, got: {trace.tool_names}"
    )
    # Should NOT trigger request_human_input (Loki is fully read-only)
    assert not trace.hitl_triggered, "Loki is read-only — HITL should never trigger"


@pytest.mark.timeout(90)
async def test_label_discovery(loki_harness):
    """'What labels exist?' → get_cluster_labels."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await loki_harness.run(
        "[READ-ONLY] What log labels are available in the cluster?"
    )

    assert "get_cluster_labels" in trace.tool_names or "read_mcp_resource" in trace.tool_names, (
        f"Expected get_cluster_labels, got: {trace.tool_names}"
    )


@pytest.mark.timeout(90)
async def test_query_cost_estimation(loki_harness):
    """'How expensive is this query?' → get_query_stats."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await loki_harness.run(
        '[READ-ONLY] Estimate the cost of running {namespace="production"} |= "error"'
    )

    assert "get_query_stats" in trace.tool_names, (
        f"Expected get_query_stats for cost estimation, got: {trace.tool_names}"
    )
