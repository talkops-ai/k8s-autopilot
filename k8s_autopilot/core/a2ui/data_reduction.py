"""Data reduction utilities for A2UI observability surfaces.

These pure-Python algorithms reduce large observability datasets to
sizes manageable by LLM context windows and frontend renderers:

- **LTTB** (Largest-Triangle-Three-Buckets): Downsamples time-series
  data while preserving visual peaks and valleys.
- **SLCT** (Simple Log Clustering Technique): Clusters log lines into
  representative templates with occurrence counts.
- **Span Pruning**: Reduces large trace spans to the most informative
  subset (root, error, and slowest spans).

No external dependencies required — all algorithms are implemented
in pure Python using only the standard library.

Reference:
    - LTTB: Sveinn Steinarsson (2013), "Downsampling Time Series for
      Visual Representation"
    - SLCT: Risto Vaarandi (2003), "A Data Clustering Algorithm for
      Mining Patterns from Event Logs"
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# ── LTTB (Largest-Triangle-Three-Buckets) ─────────────────────────────────


def lttb_downsample(
    timestamps: Sequence[float | int],
    values: Sequence[float | int],
    target_points: int = 500,
) -> list[dict[str, float]]:
    """Downsample a time-series using the LTTB algorithm.

    Preserves visual characteristics (peaks, valleys, trends) while
    reducing the number of data points.

    Parameters
    ----------
    timestamps : Sequence[float | int]
        Monotonically increasing timestamp values (epoch seconds or ms).
    values : Sequence[float | int]
        Corresponding metric values.
    target_points : int
        Maximum number of points in the output. Defaults to 500.

    Returns
    -------
    list[dict[str, float]]
        List of ``{"x": timestamp, "y": value}`` dicts, ready for
        A2UI MetricChart binding.

    Raises
    ------
    ValueError
        If timestamps and values have different lengths.
    """
    n = len(timestamps)
    if n != len(values):
        raise ValueError(
            f"timestamps ({n}) and values ({len(values)}) must have equal length"
        )

    # No downsampling needed
    if n <= target_points or target_points < 3:
        return [{"x": float(timestamps[i]), "y": float(values[i])} for i in range(n)]

    result: list[dict[str, float]] = []

    # Always include the first point
    result.append({"x": float(timestamps[0]), "y": float(values[0])})

    # Bucket size (excluding first and last points)
    bucket_size = (n - 2) / (target_points - 2)

    # Index of the previously selected point
    prev_idx = 0

    for bucket_idx in range(target_points - 2):
        # Calculate bucket boundaries
        bucket_start = int(math.floor((bucket_idx + 0) * bucket_size)) + 1
        bucket_end = int(math.floor((bucket_idx + 1) * bucket_size)) + 1
        bucket_end = min(bucket_end, n - 1)

        # Calculate the average point of the NEXT bucket (look-ahead)
        next_bucket_start = int(math.floor((bucket_idx + 1) * bucket_size)) + 1
        next_bucket_end = int(math.floor((bucket_idx + 2) * bucket_size)) + 1
        next_bucket_end = min(next_bucket_end, n)

        avg_x = 0.0
        avg_y = 0.0
        next_count = next_bucket_end - next_bucket_start
        if next_count > 0:
            for j in range(next_bucket_start, next_bucket_end):
                avg_x += float(timestamps[j])
                avg_y += float(values[j])
            avg_x /= next_count
            avg_y /= next_count
        else:
            avg_x = float(timestamps[-1])
            avg_y = float(values[-1])

        # Find the point in the current bucket with the largest triangle area
        max_area = -1.0
        max_idx = bucket_start
        prev_x = float(timestamps[prev_idx])
        prev_y = float(values[prev_idx])

        for j in range(bucket_start, bucket_end):
            # Triangle area = 0.5 * |x1(y2-y3) + x2(y3-y1) + x3(y1-y2)|
            area = abs(
                (prev_x - avg_x) * (float(values[j]) - prev_y)
                - (prev_x - float(timestamps[j])) * (avg_y - prev_y)
            )
            if area > max_area:
                max_area = area
                max_idx = j

        result.append({"x": float(timestamps[max_idx]), "y": float(values[max_idx])})
        prev_idx = max_idx

    # Always include the last point
    result.append({"x": float(timestamps[-1]), "y": float(values[-1])})

    logger.debug("LTTB: %d → %d points", n, len(result))
    return result


def downsample_series(
    series: list[dict[str, Any]],
    target_points: int = 500,
) -> list[dict[str, Any]]:
    """Downsample each series in a multi-series dataset.

    Parameters
    ----------
    series : list[dict]
        Each series has ``name``, ``data`` (list of ``{x, y}`` dicts),
        and optional ``color``.
    target_points : int
        Max points per series.

    Returns
    -------
    list[dict]
        Series with downsampled data arrays.
    """
    result = []
    for s in series:
        data = s.get("data", [])
        if not data:
            result.append(s)
            continue

        timestamps = [p["x"] for p in data]
        values = [p["y"] for p in data]
        downsampled = lttb_downsample(timestamps, values, target_points)

        result.append({**s, "data": downsampled})

    return result


# ── SLCT (Simple Log Clustering Technique) ────────────────────────────────


@dataclass
class LogTemplate:
    """A clustered log template with occurrence count."""

    template: str
    """The generalized template (tokens replaced with ``<*>``)."""

    count: int
    """Number of log lines matching this template."""

    sample: str
    """One representative raw log line."""

    severity: str = "info"
    """Most common severity level in this cluster."""


# Regex patterns for variable tokens
_VARIABLE_PATTERNS = [
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),  # IP addresses
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # UUIDs
    re.compile(r"\b[0-9a-f]{24,}\b", re.I),  # hex IDs (trace/span IDs)
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*"),  # ISO timestamps
    re.compile(r"\b\d+\.\d+\b"),  # Floating point numbers
    re.compile(r"\b\d{4,}\b"),  # Large integers (>= 4 digits)
]


def _generalize_log_line(line: str) -> str:
    """Replace variable tokens in a log line with ``<*>``."""
    result = line
    for pattern in _VARIABLE_PATTERNS:
        result = pattern.sub("<*>", result)
    return result


def cluster_log_lines(
    log_lines: list[dict[str, Any]],
    max_templates: int = 50,
) -> list[dict[str, Any]]:
    """Cluster log lines into representative templates using SLCT-style grouping.

    Parameters
    ----------
    log_lines : list[dict]
        Raw log entries. Each has at least ``message`` and optional
        ``severity``, ``timestamp``, ``labels`` keys.
    max_templates : int
        Maximum number of template clusters to return. Defaults to 50.

    Returns
    -------
    list[dict]
        Clustered templates as dicts with ``template``, ``count``,
        ``sample``, ``severity`` keys, sorted by count descending.
    """
    if not log_lines:
        return []

    # Group by generalized template
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in log_lines:
        message = entry.get("message", str(entry))
        template = _generalize_log_line(message)
        clusters[template].append(entry)

    # Build template objects sorted by frequency
    templates: list[dict[str, Any]] = []
    for template_str, entries in clusters.items():
        # Find most common severity
        severities = Counter(
            e.get("severity", e.get("level", "info")) for e in entries
        )
        most_common_severity = severities.most_common(1)[0][0] if severities else "info"

        templates.append({
            "template": template_str,
            "count": len(entries),
            "sample": entries[0].get("message", str(entries[0])),
            "severity": str(most_common_severity).lower(),
        })

    # Sort by count descending and truncate
    templates.sort(key=lambda t: t["count"], reverse=True)
    result = templates[:max_templates]

    logger.debug(
        "SLCT: %d lines → %d templates (capped at %d)",
        len(log_lines), len(templates), max_templates,
    )
    return result


# ── Trace Span Pruning ────────────────────────────────────────────────────


def prune_trace_spans(
    spans: list[dict[str, Any]],
    max_spans: int = 100,
) -> list[dict[str, Any]]:
    """Prune trace spans to the most informative subset.

    Selection priority:
    1. Root span (no parentSpanId) — always kept
    2. Error/exception spans — always kept
    3. Slowest spans (by duration) — fill remaining budget
    4. Direct children of root — fill remaining budget

    Parameters
    ----------
    spans : list[dict]
        Full span list. Each span has at least ``spanId``,
        ``parentSpanId``, ``operationName``, ``serviceName``,
        ``startTime``, ``duration``, ``status``.
    max_spans : int
        Maximum number of spans to keep. Defaults to 100.

    Returns
    -------
    list[dict]
        Pruned span list, sorted by startTime ascending.
    """
    if not spans or len(spans) <= max_spans:
        return sorted(spans, key=lambda s: s.get("startTime", 0))

    kept: dict[str, dict[str, Any]] = {}  # spanId → span
    remaining: list[dict[str, Any]] = []

    for span in spans:
        span_id = span.get("spanId", "")
        parent_id = span.get("parentSpanId")
        status = str(span.get("status", "")).lower()
        is_error = status in ("error", "unset") or span.get("statusCode") == 2

        # Priority 1: Root spans
        if not parent_id or parent_id == "":
            kept[span_id] = span
        # Priority 2: Error spans
        elif is_error:
            kept[span_id] = span
        else:
            remaining.append(span)

    budget = max_spans - len(kept)

    if budget > 0 and remaining:
        # Priority 3: Slowest spans by duration
        remaining.sort(key=lambda s: s.get("duration", 0), reverse=True)

        # Reserve half the budget for slowest, half for root children
        slow_budget = budget // 2
        child_budget = budget - slow_budget

        # Add slowest spans
        for span in remaining[:slow_budget]:
            kept[span["spanId"]] = span

        # Priority 4: Direct children of root
        root_ids = {
            sid for sid, s in kept.items()
            if not s.get("parentSpanId")
        }
        root_children = [
            s for s in remaining[slow_budget:]
            if s.get("parentSpanId") in root_ids and s["spanId"] not in kept
        ]
        for span in root_children[:child_budget]:
            kept[span["spanId"]] = span

    result = sorted(kept.values(), key=lambda s: s.get("startTime", 0))

    logger.debug("Span pruning: %d → %d spans", len(spans), len(result))
    return result


# ── Alert Aggregation ─────────────────────────────────────────────────────


def aggregate_alert_summary(
    alerts: list[dict[str, Any]],
) -> dict[str, int]:
    """Aggregate alert severity counts.

    Parameters
    ----------
    alerts : list[dict]
        Alert entries, each with a ``severity`` key.

    Returns
    -------
    dict[str, int]
        Counts keyed by severity: ``{"critical": N, "warning": N, "info": N}``.
    """
    counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for alert in alerts:
        sev = str(alert.get("severity", alert.get("labels", {}).get("severity", "info"))).lower()
        if sev in ("critical", "crit", "fatal"):
            counts["critical"] += 1
        elif sev in ("warning", "warn"):
            counts["warning"] += 1
        else:
            counts["info"] += 1
    return counts
