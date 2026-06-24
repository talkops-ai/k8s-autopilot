"""Observability A2UI surface builders.

Deterministic builders that pair fixed-schema JSON templates with
runtime data models. Each builder:

1. Loads the pre-authored component tree from ``schemas/``
2. Applies data reduction (LTTB, SLCT, span pruning) as needed
3. Emits the standard A2UI lifecycle:
   ``createSurface → updateComponents → updateDataModel``

These builders never invoke the LLM — the component tree is fixed
at author-time. Only the *data* flowing into ``updateDataModel``
changes per request.

Usage::

    from k8s_autopilot.core.a2ui.obs_surface_builder import (
        build_metric_chart_surface,
        build_obs_surface,
    )

    # Direct builder call
    ops = build_metric_chart_surface(
        surface_id="metric-cpu-checkout",
        title="CPU Usage — checkout-service",
        series=[{"name": "cpu", "data": [...]}],
        time_range={"start": "...", "end": "..."},
    )

    # Dispatch via view model kind
    ops = build_obs_surface("metric-cpu-checkout", view_model)

Reference:
    Follows the same pattern as ``surface_builder.py`` which uses
    ``copilotkit.a2ui.create_surface()`` / ``update_components()`` /
    ``update_data_model()``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from copilotkit import a2ui

from k8s_autopilot.core.a2ui.catalog_manager import OBSERVABILITY_CATALOG_ID
from k8s_autopilot.core.a2ui.data_reduction import (
    aggregate_alert_summary,
    cluster_log_lines,
    downsample_series,
    prune_trace_spans,
)
from k8s_autopilot.core.a2ui.schema_loader import load_schema
from k8s_autopilot.core.a2ui.view_models import (
    AlertsViewModel,
    LogsViewModel,
    MetricsViewModel,
    ObsViewModel,
    OTelViewModel,
    TracesViewModel,
)

logger = logging.getLogger(__name__)

# ── Load fixed schemas once at module import ──────────────────────────────

_METRIC_SCHEMA = load_schema("metric_chart_surface")
_LOG_SCHEMA = load_schema("log_table_surface")
_TRACE_SCHEMA = load_schema("trace_timeline_surface")
_ALERT_SCHEMA = load_schema("alert_status_surface")
_OTEL_SCHEMA = load_schema("otel_status_surface")

# ── Catalog ID ────────────────────────────────────────────────────────────

OBS_CATALOG_ID = OBSERVABILITY_CATALOG_ID


# ── Helper ────────────────────────────────────────────────────────────────

def _gen_surface_id(prefix: str) -> str:
    """Generate a unique surface ID with the given prefix."""
    short_id = uuid.uuid4().hex[:8]
    return f"{prefix}-{short_id}"


# ── Metric Chart ──────────────────────────────────────────────────────────

def build_metric_chart_surface(
    surface_id: str | None = None,
    title: str = "Metrics",
    series: list[dict[str, Any]] | None = None,
    time_range: dict[str, str] | None = None,
    chart_type: str = "line",
    y_axis_label: str = "Value",
    query: str = "",
    target_points: int = 500,
) -> list[dict[str, Any]]:
    """Build a metric chart A2UI surface with LTTB downsampling.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    title : str
        Chart heading.
    series : list[dict]
        Data series (auto-downsampled via LTTB).
    time_range : dict
        ``{"start": str, "end": str}`` ISO timestamps.
    chart_type : str
        ``"line"`` | ``"bar"`` | ``"area"``.
    y_axis_label : str
        Y-axis label.
    query : str
        Original PromQL/OTel query for caption.
    target_points : int
        LTTB target per series. Defaults to 500.

    Returns
    -------
    list[dict]
        A2UI operations: createSurface + updateComponents + updateDataModel.
    """
    sid = surface_id or _gen_surface_id("obs-metric")
    reduced_series = downsample_series(series or [], target_points)

    time_range = time_range or {}
    time_range_label = ""
    if time_range.get("start") and time_range.get("end"):
        time_range_label = f"{time_range['start']} → {time_range['end']}"

    return [
        a2ui.create_surface(sid, catalog_id=OBS_CATALOG_ID),
        a2ui.update_components(sid, _METRIC_SCHEMA),
        a2ui.update_data_model(sid, {
            "title": title,
            "chartType": chart_type,
            "yAxisLabel": y_axis_label,
            "series": reduced_series,
            "timeRange": time_range,
            "timeRangeLabel": time_range_label,
            "query": f"PromQL: {query}" if query else "",
        }),
    ]


# ── Log Table ─────────────────────────────────────────────────────────────

def build_log_table_surface(
    surface_id: str | None = None,
    title: str = "Logs",
    log_lines: list[dict[str, Any]] | None = None,
    query: str = "",
    max_templates: int = 50,
) -> list[dict[str, Any]]:
    """Build a log table A2UI surface with SLCT clustering.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    title : str
        Table heading.
    log_lines : list[dict]
        Raw log entries (auto-clustered via SLCT).
    query : str
        Original LogQL query for caption.
    max_templates : int
        Maximum template clusters. Defaults to 50.

    Returns
    -------
    list[dict]
        A2UI operations.
    """
    sid = surface_id or _gen_surface_id("obs-logs")
    raw_lines = log_lines or []
    total_lines = len(raw_lines)
    clustered = cluster_log_lines(raw_lines, max_templates)

    columns = [
        {"key": "severity", "label": "Level", "sortable": True},
        {"key": "template", "label": "Message Pattern", "sortable": False},
        {"key": "count", "label": "Count", "sortable": True},
        {"key": "sample", "label": "Sample", "sortable": False},
    ]

    return [
        a2ui.create_surface(sid, catalog_id=OBS_CATALOG_ID),
        a2ui.update_components(sid, _LOG_SCHEMA),
        a2ui.update_data_model(sid, {
            "title": title,
            "columns": columns,
            "rows": clustered,
            "totalLinesLabel": f"{total_lines:,} lines",
            "query": f"LogQL: {query}" if query else "",
        }),
    ]


# ── Trace Timeline ────────────────────────────────────────────────────────

def build_trace_timeline_surface(
    surface_id: str | None = None,
    title: str = "Trace",
    spans: list[dict[str, Any]] | None = None,
    service_name: str = "",
    trace_id: str = "",
    focus_span_id: str = "",
    max_spans: int = 100,
) -> list[dict[str, Any]]:
    """Build a trace timeline A2UI surface with span pruning.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    title : str
        Timeline heading.
    spans : list[dict]
        Full span list (auto-pruned).
    service_name : str
        Primary service name.
    trace_id : str
        Root trace identifier.
    focus_span_id : str
        Optional span ID to highlight.
    max_spans : int
        Maximum spans to keep. Defaults to 100.

    Returns
    -------
    list[dict]
        A2UI operations.
    """
    sid = surface_id or _gen_surface_id("obs-trace")
    raw_spans = spans or []
    total_spans = len(raw_spans)
    pruned = prune_trace_spans(raw_spans, max_spans)

    # Compute stats
    services = {s.get("serviceName", "") for s in pruned}
    services.discard("")
    error_spans = [
        s for s in pruned
        if str(s.get("status", "")).lower() in ("error", "unset")
        or s.get("statusCode") == 2
    ]

    # Root span info
    root_spans = [s for s in pruned if not s.get("parentSpanId")]
    root_status = "ok"
    root_duration = ""
    root_severity = "success"
    if root_spans:
        root = root_spans[0]
        rs = str(root.get("status", "ok")).lower()
        root_status = rs
        root_severity = "critical" if rs == "error" else "success"
        dur_ms = root.get("duration", 0)
        if dur_ms >= 1000:
            root_duration = f"{dur_ms / 1000:.1f}s"
        else:
            root_duration = f"{dur_ms}ms"

    error_severity = "critical" if error_spans else "success"

    return [
        a2ui.create_surface(sid, catalog_id=OBS_CATALOG_ID),
        a2ui.update_components(sid, _TRACE_SCHEMA),
        a2ui.update_data_model(sid, {
            "title": title or f"Trace {trace_id}",
            "spans": pruned,
            "serviceName": service_name,
            "focusSpanId": focus_span_id,
            "totalSpansLabel": f"{total_spans} spans",
            "rootSpanStatus": root_status,
            "rootSpanSeverity": root_severity,
            "rootSpanDuration": root_duration,
            "uniqueServicesCount": str(len(services)),
            "totalDuration": root_duration,
            "errorSpansCount": str(len(error_spans)),
            "errorSpanSeverity": error_severity,
        }),
    ]


# ── Alert Status ──────────────────────────────────────────────────────────

def build_alert_status_surface(
    surface_id: str | None = None,
    title: str = "Active Alerts",
    alerts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build an alert status A2UI surface.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    title : str
        Surface heading.
    alerts : list[dict]
        Alert entries with ``alertName``, ``severity``, ``summary``,
        ``sinceLabel`` keys.

    Returns
    -------
    list[dict]
        A2UI operations.
    """
    sid = surface_id or _gen_surface_id("obs-alerts")
    alert_list = alerts or []
    summary = aggregate_alert_summary(alert_list)

    # Normalize severity in each alert for StatusIndicator binding
    normalized = []
    for alert in alert_list:
        sev = str(alert.get("severity", "info")).lower()
        if sev in ("critical", "crit", "fatal"):
            sev = "critical"
        elif sev in ("warning", "warn"):
            sev = "warning"
        else:
            sev = "info"
            
        labels_dict = alert.get("labels", {})
        labels_text = ", ".join(f"{k}={v}" for k, v in labels_dict.items()) if labels_dict else ""
        
        normalized.append({**alert, "severity": sev, "labelsText": labels_text})

    return [
        a2ui.create_surface(sid, catalog_id=OBS_CATALOG_ID),
        a2ui.update_components(sid, _ALERT_SCHEMA),
        a2ui.update_data_model(sid, {
            "title": title,
            "summary": {
                "critical": str(summary["critical"]),
                "warning": str(summary["warning"]),
                "info": str(summary["info"]),
            },
            "alerts": normalized,
        }),
    ]


# ── OTel Status ───────────────────────────────────────────────────────────

def build_otel_status_surface(
    surface_id: str | None = None,
    title: str = "OpenTelemetry Status",
    columns: list[dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    overall_severity: str = "success",
    overall_status_label: str = "Status",
    overall_status_value: str = "",
) -> list[dict[str, Any]]:
    """Build an OTel status table A2UI surface.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    title : str
        Surface heading.
    columns : list[dict]
        DataTable column definitions. Defaults to Name/Status/Pipeline/Uptime.
    rows : list[dict]
        DataTable row data.
    overall_severity : str
        Overall health severity for the status badge.
    overall_status_label : str
        Status badge label.
    overall_status_value : str
        Status badge value.

    Returns
    -------
    list[dict]
        A2UI operations.
    """
    sid = surface_id or _gen_surface_id("obs-otel")

    default_columns = [
        {"key": "name", "label": "Name", "sortable": True},
        {"key": "status", "label": "Status", "sortable": True},
        {"key": "pipeline", "label": "Pipeline", "sortable": False},
        {"key": "uptime", "label": "Uptime", "sortable": True},
    ]

    return [
        a2ui.create_surface(sid, catalog_id=OBS_CATALOG_ID),
        a2ui.update_components(sid, _OTEL_SCHEMA),
        a2ui.update_data_model(sid, {
            "title": title,
            "columns": columns or default_columns,
            "rows": rows or [],
            "overallSeverity": overall_severity,
            "overallStatusLabel": overall_status_label,
            "overallStatusValue": overall_status_value,
        }),
    ]


# ── Multi-Pillar Dashboard (Tabs) ────────────────────────────────────────

def build_obs_dashboard_surface(
    surface_id: str | None = None,
    title: str = "Observability Dashboard",
    panels: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a multi-pillar dashboard using the built-in Tabs component.

    Each panel becomes a tab. Panel data is rendered as its own
    sub-component tree within the tab.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    title : str
        Dashboard heading.
    panels : list[dict]
        Panel descriptors. Each has ``tab_title``, ``kind``, and the
        relevant view model fields.

    Returns
    -------
    list[dict]
        A2UI operations.
    """
    sid = surface_id or _gen_surface_id("obs-dashboard")
    panel_list = panels or []

    # Build tab descriptors and corresponding components
    # NOTE: Explicit `list[dict[str, Any]]` annotations prevent Pyrefly from
    # narrowing the dict value type based on the first literal usage.
    tab_defs: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": {
                "Column": {
                    "children": {"explicitList": ["dashboard-title", "divider", "tabs"]}
                }
            },
        },
        {
            "id": "dashboard-title",
            "component": {
                "Text": {
                    "text": {"path": "title"},
                    "usageHint": "h2",
                }
            },
        },
        {"id": "divider", "component": {"Divider": {}}},
    ]

    for idx, panel in enumerate(panel_list):
        tab_id = f"tab-content-{idx}"
        tab_title = panel.get("tab_title", panel.get("kind", f"Tab {idx + 1}").capitalize())
        tab_defs.append({"title": tab_title, "child": tab_id})

        kind = panel.get("kind", "")
        if kind == "metrics":
            components.append({
                "id": tab_id,
                "component": {
                    "MetricChart": {
                        "chartType": {"path": f"panels/{idx}/chartType"},
                        "title": {"path": f"panels/{idx}/title"},
                        "series": {"path": f"panels/{idx}/series"},
                        "timeRange": {"path": f"panels/{idx}/timeRange"},
                        "yAxisLabel": {"path": f"panels/{idx}/yAxisLabel"},
                    }
                },
            })
        elif kind == "logs":
            components.append({
                "id": tab_id,
                "component": {
                    "DataTable": {
                        "columns": {"path": f"panels/{idx}/columns"},
                        "rows": {"path": f"panels/{idx}/rows"},
                        "searchable": True,
                    }
                },
            })
        elif kind == "traces":
            components.append({
                "id": tab_id,
                "component": {
                    "TraceTimeline": {
                        "spans": {"path": f"panels/{idx}/spans"},
                        "serviceName": {"path": f"panels/{idx}/serviceName"},
                        "showMiniMap": True,
                    }
                },
            })
        elif kind == "alerts":
            # For alerts in a dashboard tab, use a simpler column layout
            alert_col_id = f"alert-col-{idx}"
            components.append({
                "id": tab_id,
                "component": {
                    "Column": {
                        "children": {"explicitList": [f"alert-summary-{idx}", f"alert-table-{idx}"]}
                    }
                },
            })
            components.append({
                "id": f"alert-summary-{idx}",
                "component": {
                    "Text": {
                        "text": {"path": f"panels/{idx}/summaryText"},
                        "usageHint": "body",
                    }
                },
            })
            components.append({
                "id": f"alert-table-{idx}",
                "component": {
                    "DataTable": {
                        "columns": {"path": f"panels/{idx}/columns"},
                        "rows": {"path": f"panels/{idx}/rows"},
                        "searchable": True,
                    }
                },
            })
        elif kind == "otel":
            components.append({
                "id": tab_id,
                "component": {
                    "DataTable": {
                        "columns": {"path": f"panels/{idx}/columns"},
                        "rows": {"path": f"panels/{idx}/rows"},
                        "searchable": True,
                    }
                },
            })
        else:
            # Fallback: render as text
            components.append({
                "id": tab_id,
                "component": {
                    "Text": {
                        "text": {"path": f"panels/{idx}/content"},
                        "usageHint": "body",
                    }
                },
            })

    # Add the Tabs component
    components.append({
        "id": "tabs",
        "component": {
            "Tabs": {
                "tabs": tab_defs
            }
        },
    })

    # Build the data model with panel data
    panels_data = {}
    for idx, panel in enumerate(panel_list):
        panel_data = {k: v for k, v in panel.items() if k not in ("tab_title", "kind")}
        panels_data[str(idx)] = panel_data

    return [
        a2ui.create_surface(sid, catalog_id=OBS_CATALOG_ID),
        a2ui.update_components(sid, components),
        a2ui.update_data_model(sid, {
            "title": title,
            "panels": panels_data,
        }),
    ]


# ── Dispatch by view model kind ───────────────────────────────────────────

def build_obs_surface(
    surface_id: str | None,
    view_model: ObsViewModel,
) -> list[dict[str, Any]]:
    """Build an observability A2UI surface from a typed view model.

    This is the primary dispatch function. Subagents create a
    view model, and this function routes to the correct builder.

    Parameters
    ----------
    surface_id : str
        Unique surface identifier. Auto-generated if None.
    view_model : ObsViewModel
        One of the typed observability view models.

    Returns
    -------
    list[dict]
        A2UI operations.
    """
    if isinstance(view_model, MetricsViewModel):
        return build_metric_chart_surface(
            surface_id=surface_id,
            title=view_model.title,
            series=view_model.series,
            time_range=view_model.time_range,
            chart_type=view_model.chart_type,
            y_axis_label=view_model.y_axis_label,
            query=view_model.query,
        )
    elif isinstance(view_model, LogsViewModel):
        return build_log_table_surface(
            surface_id=surface_id,
            title=view_model.title,
            log_lines=view_model.rows,  # Already in row format from subagent
            query=view_model.query,
        )
    elif isinstance(view_model, TracesViewModel):
        return build_trace_timeline_surface(
            surface_id=surface_id,
            title=view_model.title,
            spans=view_model.spans,
            service_name=view_model.service_name,
            trace_id=view_model.trace_id,
            focus_span_id=view_model.focus_span_id,
        )
    elif isinstance(view_model, AlertsViewModel):
        return build_alert_status_surface(
            surface_id=surface_id,
            title=view_model.title,
            alerts=view_model.alerts,
        )
    elif isinstance(view_model, OTelViewModel):
        return build_otel_status_surface(
            surface_id=surface_id,
            title=view_model.title,
            columns=view_model.columns,
            rows=view_model.rows,
            overall_severity=view_model.overall_severity,
            overall_status_label=view_model.overall_status_label,
            overall_status_value=view_model.overall_status_value,
        )
    else:
        logger.warning("Unknown view model kind: %s", type(view_model).__name__)
        return []


# ── Serialize operations to JSON string ───────────────────────────────────

def serialize_a2ui_ops(ops: list[dict[str, Any]]) -> str:
    """Serialize A2UI operations to a JSON string for tool return.

    Wraps the operations in the ``a2ui_operations`` key that the
    A2UI middleware detects and forwards to the frontend.

    Parameters
    ----------
    ops : list[dict]
        A2UI operations from any builder.

    Returns
    -------
    str
        JSON string: ``{"a2ui_operations": [...]}``.
    """
    return json.dumps({a2ui.A2UI_OPERATIONS_KEY: ops})
