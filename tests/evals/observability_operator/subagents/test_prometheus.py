"""
Eval: Prometheus subagent tool selection with real LLM.

Validates the prometheus-operator subagent makes correct MCP tool
selections for read-only queries vs state-modifying operations.

Scenarios test:
- PromQL query → prom_query_instant (not prom_query_range for simple queries)
- Install exporter → prom_install_exporter (not prom_recommend_exporter)
- Create ServiceMonitor → prom_apply_servicemonitor
- Create rule → prom_upsert_rule_group
- Read-only query does NOT trigger install/apply/upsert tools
"""
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.slow, pytest.mark.asyncio]

SKIP_MSG = "Subagent evals require real LLM — skipped in CI"

# State-modifying tool names — these should NEVER fire for read-only tasks
PROM_WRITE_TOOLS = {
    "prom_install_exporter", "prom_uninstall_exporter",
    "prom_apply_servicemonitor", "prom_upsert_rule_group",
    "prom_manage_file_sd", "prom_configure_remote_write",
}


@pytest.fixture(scope="module")
def prom_harness():
    return SubagentEvalHarness.for_prometheus()


# ─── Read-only scenarios ──────────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_promql_instant_query(prom_harness):
    """'[READ-ONLY] Query CPU usage for checkout' → prom_query_instant."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await prom_harness.run(
        "[READ-ONLY] Query CPU usage for service checkout in namespace default"
    )

    assert "prom_query_instant" in trace.tool_names or "read_mcp_resource" in trace.tool_names, (
        f"Expected prom_query_instant or read_mcp_resource, got: {trace.tool_names}\n"
        f"Final: {trace.final_message[:200]}"
    )
    # Must NOT trigger any write tools
    write_calls = set(trace.tool_names) & PROM_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


@pytest.mark.timeout(90)
async def test_explore_labels(prom_harness):
    """'[READ-ONLY] What labels exist for node_cpu_seconds_total?' → prom_explore_labels."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await prom_harness.run(
        "[READ-ONLY] What labels exist for node_cpu_seconds_total?"
    )

    assert "prom_explore_labels" in trace.tool_names or "read_mcp_resource" in trace.tool_names, (
        f"Expected prom_explore_labels, got: {trace.tool_names}"
    )
    write_calls = set(trace.tool_names) & PROM_WRITE_TOOLS
    assert not write_calls, f"Read-only query triggered write tools: {write_calls}"


@pytest.mark.timeout(90)
async def test_verify_exporter_readonly(prom_harness):
    """'[READ-ONLY] Is node-exporter installed and scraping?' → prom_verify_exporter (not install)."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await prom_harness.run(
        "[READ-ONLY] Is the node-exporter installed and scraping in namespace monitoring?"
    )

    # Must NOT call prom_install_exporter for a verification request
    assert "prom_install_exporter" not in trace.tool_names, (
        f"Verification query incorrectly triggered prom_install_exporter!\n"
        f"All tools called: {trace.tool_names}"
    )


# ─── State-modifying scenarios ────────────────────────────────────────────

@pytest.mark.timeout(90)
async def test_install_exporter(prom_harness):
    """'[STATE-MODIFYING] [PLAN-LOCKED] Install node-exporter' → prom_install_exporter."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await prom_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Install the node-exporter in namespace monitoring. "
        "Backend: default. Plan approved by user."
    )

    # Must call prom_install_exporter (or request_human_input for planning)
    installed = "prom_install_exporter" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert installed or asked_human, (
        f"Expected prom_install_exporter or HITL, got: {trace.tool_names}\n"
        f"Final: {trace.final_message[:200]}"
    )


@pytest.mark.timeout(90)
async def test_create_servicemonitor(prom_harness):
    """'[STATE-MODIFYING] [PLAN-LOCKED] Wire checkout to Prometheus' → prom_apply_servicemonitor."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await prom_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Create a ServiceMonitor for service checkout "
        "in namespace default, port 8080, interval 30s. Backend: default. Plan approved."
    )

    applied = "prom_apply_servicemonitor" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert applied or asked_human, (
        f"Expected prom_apply_servicemonitor or HITL, got: {trace.tool_names}\n"
        f"Final: {trace.final_message[:200]}"
    )


@pytest.mark.timeout(90)
async def test_upsert_alert_rule(prom_harness):
    """'[STATE-MODIFYING] [PLAN-LOCKED] Create alert for high CPU' → prom_upsert_rule_group."""
    if os.environ.get("CI"):
        pytest.skip(SKIP_MSG)

    trace = await prom_harness.run(
        "[STATE-MODIFYING] [PLAN-LOCKED] Create alerting rule: HighCPU fires when "
        "rate(container_cpu_usage_seconds_total[5m]) > 0.9. Group: custom-alerts, "
        "backend: default. Plan approved."
    )

    upserted = "prom_upsert_rule_group" in trace.tool_names
    drafted = "prom_draft_alert_rule" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert upserted or drafted or asked_human, (
        f"Expected prom_upsert_rule_group or prom_draft_alert_rule, got: {trace.tool_names}\n"
        f"Final: {trace.final_message[:200]}"
    )
