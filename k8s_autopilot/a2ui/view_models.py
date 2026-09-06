"""Typed view-model dataclasses for observability A2UI surfaces.

Each view model represents the data contract between a subagent
(which populates the data) and the surface builder (which renders it).

Subagents fill these models; the coordinator or the subagent itself
passes them to ``obs_surface_builder`` for deterministic rendering.

These are *transport objects* — they carry reduced, UI-ready data.
Raw observability data (full PromQL results, raw log streams) should
be processed through ``data_reduction`` before populating these models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricsViewModel:
    """View model for Prometheus / OTel metric chart surfaces.

    Attributes:
        kind: Discriminator, always ``"metrics"``.
        title: Chart heading (e.g., ``"CPU Usage — checkout-service"``).
        chart_type: ``"line"`` | ``"bar"`` | ``"area"``.
        series: LTTB-downsampled series list. Each series:
            ``{"name": str, "data": [{"x": float, "y": float}, ...], "color": str?}``
        time_range: ``{"start": str, "end": str}`` ISO timestamps.
        y_axis_label: Y-axis label (e.g., ``"% Utilization"``, ``"req/s"``).
        query: Original PromQL / OTel query string for caption.
    """

    kind: str = "metrics"
    title: str = ""
    chart_type: str = "line"
    series: list[dict[str, Any]] = field(default_factory=list)
    time_range: dict[str, str] = field(default_factory=dict)
    y_axis_label: str = "Value"
    query: str = ""


@dataclass
class LogsViewModel:
    """View model for Loki log table surfaces.

    Attributes:
        kind: Discriminator, always ``"logs"``.
        title: Table heading (e.g., ``"Errors — payment-service"``).
        columns: Column definitions for the DataTable component.
            Each: ``{"key": str, "label": str, "sortable": bool?}``
        rows: SLCT-clustered or raw row data matching column keys.
        total_lines: Total number of log lines before reduction.
        query: Original LogQL query string for caption.
    """

    kind: str = "logs"
    title: str = ""
    columns: list[dict[str, Any]] = field(default_factory=lambda: [
        {"key": "severity", "label": "Level", "sortable": True},
        {"key": "template", "label": "Message Pattern", "sortable": False},
        {"key": "count", "label": "Count", "sortable": True},
        {"key": "sample", "label": "Sample", "sortable": False},
    ])
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_lines: int = 0
    query: str = ""


@dataclass
class TracesViewModel:
    """View model for Tempo trace timeline surfaces.

    Attributes:
        kind: Discriminator, always ``"traces"``.
        title: Timeline heading (e.g., ``"Trace abc123 — checkout-service"``).
        spans: Pruned span array. Each span:
            ``{"spanId": str, "parentSpanId": str?, "operationName": str,
              "serviceName": str, "startTime": int, "duration": int, "status": str}``
        service_name: Primary service name.
        trace_id: Root trace identifier.
        total_spans: Total span count before pruning.
        focus_span_id: Optional span ID to highlight.
        root_span_status: ``"ok"`` | ``"error"`` | ``"unset"``.
        root_span_duration: Human-readable duration (e.g., ``"1.2s"``).
        unique_services: Number of distinct services in the trace.
        error_spans_count: Number of error spans.
    """

    kind: str = "traces"
    title: str = ""
    spans: list[dict[str, Any]] = field(default_factory=list)
    service_name: str = ""
    trace_id: str = ""
    total_spans: int = 0
    focus_span_id: str = ""
    root_span_status: str = "ok"
    root_span_duration: str = ""
    unique_services: int = 0
    error_spans_count: int = 0


@dataclass
class AlertsViewModel:
    """View model for Alertmanager alert status surfaces.

    Attributes:
        kind: Discriminator, always ``"alerts"``.
        title: Surface heading (e.g., ``"Active Alerts — production"``).
        alerts: Alert entries. Each:
            ``{"alertName": str, "severity": str, "summary": str,
              "sinceLabel": str, "labels": dict?}``
        summary: Severity counts ``{"critical": int, "warning": int, "info": int}``.
    """

    kind: str = "alerts"
    title: str = ""
    alerts: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=lambda: {
        "critical": 0, "warning": 0, "info": 0,
    })


@dataclass
class OTelViewModel:
    """View model for OpenTelemetry status surfaces.

    Covers collector listings, instrumented service tables, and
    cardinality reports.

    Attributes:
        kind: Discriminator, always ``"otel"``.
        title: Surface heading (e.g., ``"OTel Collectors"``).
        columns: DataTable column definitions.
        rows: DataTable row data.
        overall_severity: ``"success"`` | ``"warning"`` | ``"critical"`` | ``"info"``.
        overall_status_label: Status badge label (e.g., ``"All Healthy"``).
        overall_status_value: Status badge value (e.g., ``"5/5 running"``).
    """

    kind: str = "otel"
    title: str = ""
    columns: list[dict[str, Any]] = field(default_factory=lambda: [
        {"key": "name", "label": "Name", "sortable": True},
        {"key": "status", "label": "Status", "sortable": True},
        {"key": "pipeline", "label": "Pipeline", "sortable": False},
        {"key": "uptime", "label": "Uptime", "sortable": True},
    ])
    rows: list[dict[str, Any]] = field(default_factory=list)
    overall_severity: str = "success"
    overall_status_label: str = "Status"
    overall_status_value: str = ""


# ── Type alias for dispatch ───────────────────────────────────────────────

ObsViewModel = (
    MetricsViewModel
    | LogsViewModel
    | TracesViewModel
    | AlertsViewModel
    | OTelViewModel
)
"""Union type for all observability view models."""
