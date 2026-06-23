"""LangChain tools for building observability A2UI surfaces.

These tools are injected into the observability subagents and coordinator
so the LLM can render observability data as rich A2UI dashboards.

Tools:
    ``build_obs_a2ui``: Single-pillar surface builder (subagent use).
    ``build_obs_dashboard``: Multi-pillar tabbed dashboard (coordinator use).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool as langchain_tool
from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime

from k8s_autopilot.core.a2ui.obs_surface_builder import (
    build_alert_status_surface,
    build_log_table_surface,
    build_metric_chart_surface,
    build_obs_dashboard_surface,
    build_otel_status_surface,
    build_trace_timeline_surface,
    serialize_a2ui_ops,
)

logger = logging.getLogger(__name__)


@langchain_tool
def build_obs_a2ui(kind: str, data: str, runtime: ToolRuntime) -> str:
    """Build an A2UI surface for observability query results.

    Renders observability data as rich interactive dashboards using
    the A2UI protocol. Data reduction (LTTB downsampling for metrics,
    SLCT clustering for logs, span pruning for traces) is applied
    automatically.

    Args:
        kind: The type of observability data. One of:
            ``"metrics"`` — Time-series chart (MetricChart component)
            ``"logs"`` — Searchable log table (DataTable component)
            ``"traces"`` — Trace waterfall timeline (TraceTimeline component)
            ``"alerts"`` — Alert status grid (StatusIndicator components)
            ``"otel"`` — OTel collector/service table (DataTable component)
        data: JSON string containing the visualization data, or "__USE_ARTIFACT__"
            to pull buffered data from the agent state.
        runtime: Injected tool runtime context.

    Returns:
        JSON string containing A2UI operations for the middleware to
        forward to the frontend renderer.
    """
    if data == "__USE_ARTIFACT__":
        parsed = None
        messages = runtime.state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage) and msg.name != "build_obs_a2ui":
                if isinstance(msg.artifact, dict) and "a2ui_buffered_data" in msg.artifact:
                    parsed = msg.artifact["a2ui_buffered_data"]
                    logger.debug("build_obs_a2ui: Successfully loaded buffered data from tool: %s", msg.name)
                    break
        if parsed is None:
            return json.dumps({"error": "Failed to find buffered A2UI data in previous tool messages."})
    else:
        try:
            parsed = json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError as e:
            logger.error("build_obs_a2ui: invalid JSON data: %s", e)
            return json.dumps({"error": f"Invalid JSON: {e}"})

    title = parsed.get("title", kind.capitalize())

    try:
        if kind == "metrics":
            ops = build_metric_chart_surface(
                title=title,
                series=parsed.get("series", []),
                time_range=parsed.get("timeRange", {}),
                chart_type=parsed.get("chartType", "line"),
                y_axis_label=parsed.get("yAxisLabel", "Value"),
                query=parsed.get("query", ""),
            )
        elif kind == "logs":
            ops = build_log_table_surface(
                title=title,
                log_lines=parsed.get("logLines", parsed.get("rows", [])),
                query=parsed.get("query", ""),
            )
        elif kind == "traces":
            ops = build_trace_timeline_surface(
                title=title,
                spans=parsed.get("spans", []),
                service_name=parsed.get("serviceName", ""),
                trace_id=parsed.get("traceId", ""),
                focus_span_id=parsed.get("focusSpanId", ""),
            )
        elif kind == "alerts":
            ops = build_alert_status_surface(
                title=title,
                alerts=parsed.get("alerts", []),
            )
        elif kind == "otel":
            ops = build_otel_status_surface(
                title=title,
                columns=parsed.get("columns"),
                rows=parsed.get("rows", []),
                overall_severity=parsed.get("overallSeverity", "success"),
                overall_status_label=parsed.get("overallStatusLabel", "Status"),
                overall_status_value=parsed.get("overallStatusValue", ""),
            )
        else:
            return json.dumps({"error": f"Unknown kind: {kind}. Expected: metrics, logs, traces, alerts, otel"})

        return serialize_a2ui_ops(ops)

    except Exception as e:
        logger.exception("build_obs_a2ui failed for kind=%s", kind)
        return json.dumps({"error": f"Surface builder failed: {e}"})


@langchain_tool
def build_obs_dashboard(kind: str, data: str, runtime: ToolRuntime) -> str:
    """Build a multi-pillar tabbed observability dashboard.

    Creates a tabbed dashboard with one tab per observability pillar.
    Used by the coordinator for cross-domain investigations that span
    multiple subagents (e.g., "investigate checkout errors" which needs
    metrics + logs + traces).

    Args:
        kind: Must be ``"dashboard"``.
        data: JSON string with ``title`` and ``panels`` array, or "__USE_ARTIFACT__"
            to pull buffered data from the agent state.
        runtime: Injected tool runtime context.

    Returns:
        JSON string containing A2UI operations.
    """
    if data == "__USE_ARTIFACT__":
        parsed = None
        messages = runtime.state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage) and msg.name != "build_obs_dashboard":
                if isinstance(msg.artifact, dict) and "a2ui_buffered_data" in msg.artifact:
                    parsed = msg.artifact["a2ui_buffered_data"]
                    logger.debug("build_obs_dashboard: Successfully loaded buffered data from tool: %s", msg.name)
                    break
        if parsed is None:
            return json.dumps({"error": "Failed to find buffered dashboard data in previous tool messages."})
    else:
        try:
            parsed = json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError as e:
            logger.error("build_obs_dashboard: invalid JSON data: %s", e)
            return json.dumps({"error": f"Invalid JSON: {e}"})

    try:
        ops = build_obs_dashboard_surface(
            title=parsed.get("title", "Observability Dashboard"),
            panels=parsed.get("panels", []),
        )
        return serialize_a2ui_ops(ops)
    except Exception as e:
        logger.exception("build_obs_dashboard failed")
        return json.dumps({"error": f"Dashboard builder failed: {e}"})


def create_obs_a2ui_tools() -> list:
    """Return the list of observability A2UI tools for subagent injection.

    Returns:
        List containing the ``build_obs_a2ui`` tool.
    """
    return [build_obs_a2ui]


def create_obs_dashboard_tools() -> list:
    """Return the list of dashboard A2UI tools for coordinator injection.

    Returns:
        List containing both ``build_obs_a2ui`` and ``build_obs_dashboard`` tools.
    """
    return [build_obs_a2ui, build_obs_dashboard]
