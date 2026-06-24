"""
Context Probe — Lightweight cluster state discovery for dynamic prompt suggestions.

Uses the existing MCPClient to run read-only tool calls against the cluster,
then generates skill-scoped prompt suggestions based on real data.

Architecture
~~~~~~~~~~~~
    MCPClient.connect() → parallel tool calls → template renderer → JSON

Design decisions
~~~~~~~~~~~~~~~~
- **No LLM call** — template-based for speed and determinism (<2s).
- **Graceful degradation** — if a server is unreachable (e.g. ArgoCD not
  installed), the corresponding skill card is suppressed.
- **Parallel probes** — all MCP calls run concurrently via asyncio.gather.
- **Read-only** — only uses listing/status tools, never mutates state.

Usage::

    from k8s_autopilot.core.context_probe import ContextProbe

    probe = ContextProbe(config)
    result = await probe.run()
    # → {"skills": [...], "probed_at": "2026-06-01T16:00:00Z"}
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from k8s_autopilot.utils.mcp_client import MCPClient, create_mcp_client
from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("ContextProbe")


# ── Probe result types ───────────────────────────────────────────────

class ProbeResult:
    """Container for raw probe data from a single MCP tool call."""

    __slots__ = ("data", "error", "server_available")

    def __init__(
        self,
        data: Any = None,
        error: Optional[str] = None,
        server_available: bool = True,
    ):
        self.data = data
        self.error = error
        self.server_available = server_available


# ── Main Probe ───────────────────────────────────────────────────────

class ContextProbe:
    """
    Probes cluster state via MCP tool calls and generates skill-scoped
    prompt suggestions.

    Each probe runs in its own MCP client context to isolate failures
    (e.g. ArgoCD server down should not prevent Helm probes).
    """

    def __init__(self, config: "Config") -> None:
        self._config = config

    async def run(self) -> Dict[str, Any]:
        """
        Run all probes in parallel and return structured suggestions.

        Returns:
            Dict with ``skills`` list and ``probed_at`` timestamp.
            Each skill has ``id``, ``name``, and ``examples``.
            Skills for unavailable services are omitted.
        """
        try:
            results = await asyncio.wait_for(
                self._run_all_probes(),
                timeout=12.0,  # Hard cap for all probes combined
            )
        except asyncio.TimeoutError:
            logger.warning("Context probe timed out after 12s — returning empty")
            return {"skills": [], "error": "timeout", "probed_at": _now()}
        except Exception as exc:
            logger.error("Context probe failed", extra={"error": str(exc)})
            return {"skills": [], "error": str(exc), "probed_at": _now()}

        return self._build_response(results)

    # ── Parallel probe runner ────────────────────────────────────────

    async def _run_all_probes(self) -> Dict[str, ProbeResult]:
        """Run all domain probes concurrently."""
        probe_tasks = {
            "namespaces": self._probe_namespaces(),
            "helm_releases": self._probe_helm_releases(),
            "problem_pods": self._probe_problem_pods(),
            "argocd_apps": self._probe_argocd_apps(),
            "argo_rollouts": self._probe_argo_rollouts(),
            "prometheus": self._probe_prometheus(),
            "alertmanager": self._probe_alertmanager(),
            "traefik": self._probe_traefik(),
            # ── Future MCP servers (placeholders) ──
            "tempo": self._probe_tempo(),
            "loki": self._probe_loki(),
            "otel": self._probe_otel(),
        }

        keys = list(probe_tasks.keys())
        raw_results = await asyncio.gather(
            *probe_tasks.values(),
            return_exceptions=True,
        )

        results: Dict[str, ProbeResult] = {}
        for key, result in zip(keys, raw_results):
            if isinstance(result, BaseException):
                logger.debug(
                    f"Probe '{key}' raised exception",
                    extra={"error": str(result)},
                )
                results[key] = ProbeResult(
                    error=str(result), server_available=False
                )
            elif isinstance(result, ProbeResult):
                results[key] = result

        return results

    # ── Individual probes ────────────────────────────────────────────

    async def _probe_namespaces(self) -> ProbeResult:
        """List cluster namespaces via K8s MCP."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["kubernetes_mcp_server"]
            ) as client:
                tool = client.get_tool("namespaces_list")
                if not tool:
                    tool = client.get_tool("kubernetes_list_namespaces")
                if not tool:
                    return ProbeResult(error="no namespace tool found")

                raw = await tool.ainvoke({})
                namespaces = _parse_namespace_list(raw)
                logger.debug(
                    "Namespace probe complete",
                    extra={"count": len(namespaces)},
                )
                return ProbeResult(data=namespaces)
        except Exception as exc:
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_helm_releases(self) -> ProbeResult:
        """List Helm releases via Helm MCP."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["helm_mcp_server"]
            ) as client:
                tool = client.get_tool("kubernetes_get_helm_releases")
                if not tool:
                    tool = client.get_tool("helm_get_release_status")
                if not tool:
                    return ProbeResult(error="no helm list tool found")

                raw = await tool.ainvoke({})
                releases = _parse_helm_releases(raw)
                logger.debug(
                    "Helm release probe complete",
                    extra={"count": len(releases)},
                )
                return ProbeResult(data=releases)
        except Exception as exc:
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_problem_pods(self) -> ProbeResult:
        """Find pods that are not Running via K8s MCP."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["kubernetes_mcp_server"]
            ) as client:
                tool = client.get_tool("pods_list")
                if not tool:
                    return ProbeResult(data=[])

                raw = await tool.ainvoke({})
                pods = _parse_problem_pods(raw)
                logger.debug(
                    "Problem pods probe complete",
                    extra={"count": len(pods)},
                )
                return ProbeResult(data=pods)
        except Exception as exc:
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_argocd_apps(self) -> ProbeResult:
        """List ArgoCD applications (if server is available)."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["argocd_mcp_server"]
            ) as client:
                tool = client.get_tool("list_applications")
                if not tool:
                    return ProbeResult(error="no argocd tool found")

                # ArgoCD list_applications requires cluster_name.
                # Try the standard in-cluster reference first.
                raw = await tool.ainvoke({
                    "cluster_name": "https://kubernetes.default.svc",
                })
                apps = _parse_argocd_apps(raw)

                # If no apps found, try 'in-cluster' alias
                if not apps:
                    try:
                        raw2 = await tool.ainvoke({
                            "cluster_name": "in-cluster",
                        })
                        apps = _parse_argocd_apps(raw2)
                    except Exception:
                        pass  # Keep the first result

                logger.debug(
                    "ArgoCD probe complete",
                    extra={"count": len(apps)},
                )
                return ProbeResult(data=apps)
        except Exception as exc:
            # ArgoCD server not installed/reachable — expected in many envs
            logger.debug("ArgoCD probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_prometheus(self) -> ProbeResult:
        """Check Prometheus connectivity and get firing alerts."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["prometheus-mcp-server"]
            ) as client:
                # Try to get firing alerts as a quick health/context check
                tool = client.get_tool("prom_query_instant")
                if not tool:
                    return ProbeResult(data={"connected": True, "alerts": []})

                raw = await tool.ainvoke({
                    "backend_id": os.environ.get("PROMETHEUS_BACKEND_ID", "default"),
                    "query": "ALERTS{alertstate='firing'}",
                })
                alerts = _parse_prom_alerts(raw)
                logger.debug(
                    "Prometheus probe complete",
                    extra={"alerts": len(alerts)},
                )
                return ProbeResult(data={"connected": True, "alerts": alerts})
        except Exception as exc:
            logger.debug("Prometheus probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_alertmanager(self) -> ProbeResult:
        """Check AlertManager connectivity."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["alertmanager-mcp-server"]
            ) as client:
                # Just verify connectivity — the server exists
                tools = client.get_tools()
                logger.debug(
                    "AlertManager probe complete",
                    extra={"tools": len(tools)},
                )
                return ProbeResult(data={"connected": True, "tool_count": len(tools)})
        except Exception as exc:
            logger.debug("AlertManager probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_traefik(self) -> ProbeResult:
        """Check Traefik MCP server availability."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["traefik_mcp_server"]
            ) as client:
                tools = client.get_tools()
                logger.debug(
                    "Traefik probe complete",
                    extra={"tools": len(tools)},
                )
                return ProbeResult(data={"connected": True, "tool_count": len(tools)})
        except Exception as exc:
            logger.debug("Traefik probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_argo_rollouts(self) -> ProbeResult:
        """Check Argo Rollouts MCP server availability."""
        try:
            async with create_mcp_client(
                self._config, server_filter=["argo_rollout_mcp_server"]
            ) as client:
                tools = client.get_tools()
                logger.debug(
                    "Argo Rollouts probe complete",
                    extra={"tools": len(tools)},
                )
                return ProbeResult(data={"connected": True, "tool_count": len(tools)})
        except Exception as exc:
            logger.debug("Argo Rollouts probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    # ── Placeholder probes (future MCP servers) ──────────────────────
    # These return server_available=False until the MCP servers are
    # hooked into the config. Once connected, they will surface
    # traceability and app monitoring suggestions.

    async def _probe_tempo(self) -> ProbeResult:
        """Check Tempo (distributed tracing) MCP server availability.
        
        Fetches backends to surface dynamic trace suggestions.
        """
        try:
            async with create_mcp_client(
                self._config, server_filter=["tempo-mcp-server"]
            ) as client:
                tool = client.get_tool("tempo_list_backends")
                if not tool:
                    return ProbeResult(data={"connected": True, "backends": []})

                raw = await tool.ainvoke({})
                data = _safe_json(raw)
                backends = []
                if isinstance(data, dict):
                    backends = data.get("backends", [])
                elif isinstance(data, list):
                    backends = data

                logger.debug(
                    "Tempo probe complete",
                    extra={"backends": len(backends)},
                )
                return ProbeResult(data={"connected": True, "backends": backends})
        except Exception as exc:
            logger.debug("Tempo probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_loki(self) -> ProbeResult:
        """Check Loki (log aggregation) MCP server availability.

        Fetches available cluster labels to surface dynamic log queries.
        """
        try:
            async with create_mcp_client(
                self._config, server_filter=["loki-mcp-server"]
            ) as client:
                tool = client.get_tool("get_cluster_labels")
                if not tool:
                    return ProbeResult(data={"connected": True, "labels": []})

                raw = await tool.ainvoke({
                    "start": "now-24h",
                    "end": "now"
                })
                data = _safe_json(raw)
                labels = []
                if isinstance(data, dict):
                    labels = data.get("labels", [])

                logger.debug(
                    "Loki probe complete",
                    extra={"labels": len(labels)},
                )
                return ProbeResult(data={"connected": True, "labels": labels})
        except Exception as exc:
            logger.debug("Loki probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    async def _probe_otel(self) -> ProbeResult:
        """Check OpenTelemetry Collector MCP server availability.

        Fetches collectors to surface dynamic collector analysis suggestions.
        """
        try:
            async with create_mcp_client(
                self._config, server_filter=["opentelemetry-mcp-server"]
            ) as client:
                tool = client.get_tool("otel_list_collectors")
                if not tool:
                    return ProbeResult(data={"connected": True, "collectors": []})

                raw = await tool.ainvoke({})
                data = _safe_json(raw)
                
                collectors = []
                if isinstance(data, dict):
                    raw_items = data.get("items", data.get("collectors", []))
                    if isinstance(raw_items, list):
                        collectors = raw_items

                logger.debug(
                    "OpenTelemetry probe complete",
                    extra={"collectors": len(collectors)},
                )
                return ProbeResult(data={"connected": True, "collectors": collectors})
        except Exception as exc:
            logger.debug("OpenTelemetry probe skipped (server unavailable)")
            return ProbeResult(error=str(exc), server_available=False)

    # ── Response builder ─────────────────────────────────────────────

    def _build_response(
        self, results: Dict[str, ProbeResult]
    ) -> Dict[str, Any]:
        """Transform probe results into skill-scoped suggestions."""
        skills: List[Dict[str, Any]] = []

        # Helm Operator — always included (core capability)
        skills.append(
            _build_helm_suggestions(
                results.get("namespaces", ProbeResult()),
                results.get("helm_releases", ProbeResult()),
            )
        )

        # K8s Operator — always included (core capability)
        skills.append(
            _build_k8s_suggestions(
                results.get("namespaces", ProbeResult()),
                results.get("problem_pods", ProbeResult()),
            )
        )

        # App Operator — only if ArgoCD, Argo Rollouts, or Traefik is reachable
        app_skill = _build_app_suggestions(
            results.get("argocd_apps", ProbeResult()),
            results.get("argo_rollouts", ProbeResult()),
            results.get("traefik", ProbeResult()),
        )
        if app_skill:
            skills.append(app_skill)

        # Observability — Prometheus, AlertManager, + future Tempo/Loki/OTel
        obs_skill = _build_observability_suggestions(
            results.get("prometheus", ProbeResult()),
            results.get("alertmanager", ProbeResult()),
            results.get("tempo", ProbeResult()),
            results.get("loki", ProbeResult()),
            results.get("otel", ProbeResult()),
        )
        if obs_skill:
            skills.append(obs_skill)

        return {"skills": skills, "probed_at": _now()}


# ── Parser helpers ───────────────────────────────────────────────────
# Each parser gracefully handles string, dict, or list responses
# from MCP tools, extracting what we need.

_SYSTEM_NS = frozenset({
    "default", "kube-system", "kube-public", "kube-node-lease",
    "local-path-storage",
})


def _safe_json(raw: Any) -> Any:
    """Parse JSON if raw is a string, otherwise return as-is."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _parse_tabular_text(text: str) -> List[Dict[str, str]]:
    """
    Parse kubectl-style tabular text output into a list of dicts.

    Handles the common MCP output format::

        APIVERSION   KIND        NAME           STATUS   AGE
        v1           Namespace   cert-manager   Active   3d10h

    Returns a list of row dicts keyed by lowercase header names.
    """
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return []

    # Parse header to get column positions
    header_line = lines[0]
    headers: List[str] = []
    col_starts: List[int] = []

    i = 0
    while i < len(header_line):
        # Skip whitespace
        while i < len(header_line) and header_line[i] == ' ':
            i += 1
        if i >= len(header_line):
            break
        col_start = i
        # Read header word
        while i < len(header_line) and header_line[i] != ' ':
            i += 1
        header_word = header_line[col_start:i].strip().lower()
        headers.append(header_word)
        col_starts.append(col_start)

    rows: List[Dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        row: Dict[str, str] = {}
        for j, hdr in enumerate(headers):
            start = col_starts[j]
            end = col_starts[j + 1] if j + 1 < len(col_starts) else len(line)
            val = line[start:end].strip() if start < len(line) else ""
            row[hdr] = val
        rows.append(row)

    return rows


def _parse_namespace_list(raw: Any) -> List[str]:
    """
    Extract namespace names from MCP tool output.

    Handles:
        - kubectl-style tabular text (APIVERSION KIND NAME STATUS AGE LABELS)
        - JSON dict with items[].metadata.name
        - JSON list of strings
    """
    data = _safe_json(raw)

    # Handle kubectl-style JSON output
    if isinstance(data, dict):
        items = data.get("items", [])
        return [
            item.get("metadata", {}).get("name", "")
            for item in items
            if item.get("metadata", {}).get("name")
        ]

    # Handle simple list of strings or dicts
    if isinstance(data, list):
        if data and isinstance(data[0], str):
            return data
        return [
            item.get("name", "") if isinstance(item, dict) else str(item)
            for item in data
        ]

    # Handle tabular text (kubectl output)
    if isinstance(data, str):
        rows = _parse_tabular_text(data)
        if rows and "name" in rows[0]:
            return [r["name"] for r in rows if r.get("name")]
        # Fallback: simple line-per-namespace
        return [line.strip() for line in data.splitlines() if line.strip()]

    return []


def _parse_helm_releases(raw: Any) -> List[Dict[str, str]]:
    """Extract release info from MCP tool output."""
    data = _safe_json(raw)

    releases: List[Dict[str, str]] = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name: str = item.get("name") or item.get("release_name") or ""
                ns: str = item.get("namespace") or "default"
                status: str = item.get("status") or item.get("state") or "unknown"
                chart: str = item.get("chart") or ""
                releases.append({
                    "name": name,
                    "namespace": ns,
                    "status": status,
                    "chart": chart,
                })
    elif isinstance(data, dict):
        # Single release or wrapped response
        raw_items: Any = data.get("releases", data.get("items", [data]))
        iter_items: List[Any] = raw_items if isinstance(raw_items, list) else [raw_items]
        for item in iter_items:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("release_name") or "")
                ns = str(item.get("namespace") or "default")
                status = str(item.get("status") or "unknown")
                chart = str(item.get("chart") or "")
                releases.append({
                    "name": name,
                    "namespace": ns,
                    "status": status,
                    "chart": chart,
                })
    elif isinstance(data, str):
        # Try parsing lines (helm list output)
        for line in data.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in ("NAME", "---"):
                releases.append({
                    "name": parts[0],
                    "namespace": parts[1] if len(parts) > 1 else "default",
                    "status": parts[3] if len(parts) > 3 else "unknown",
                    "chart": parts[2] if len(parts) > 2 else "",
                })

    return releases


def _parse_problem_pods(raw: Any) -> List[Dict[str, str]]:
    """
    Extract pods with issues from MCP tool output.

    Handles:
        - kubectl-style tabular text (NAMESPACE APIVERSION KIND NAME READY STATUS ...)
        - JSON dict/list formats
    """
    data = _safe_json(raw)

    # Tabular text format from pods_list
    if isinstance(data, str):
        rows = _parse_tabular_text(data)
        _PROBLEM_STATUSES = {
            "CrashLoopBackOff", "Error", "ImagePullBackOff",
            "ErrImagePull", "CreateContainerError", "OOMKilled",
            "Pending", "Failed", "Unknown", "Terminating",
            "ContainerStatusUnknown",
        }
        pods = []
        for row in rows:
            status = row.get("status", "")
            namespace = row.get("namespace", "")
            name = row.get("name", "")
            if (
                status not in ("Running", "Succeeded", "Completed")
                and namespace not in _SYSTEM_NS
                and name
            ):
                pods.append({
                    "name": name,
                    "namespace": namespace,
                    "status": status,
                })
        return pods[:5]

    # JSON dict/list formats (kubectl -o json)
    pods: List[Dict[str, str]] = []
    all_pods: List[Any] = []
    if isinstance(data, dict):
        raw_pods: Any = data.get("items", data.get("pods", []))
        all_pods = raw_pods if isinstance(raw_pods, list) else []
    elif isinstance(data, list):
        all_pods = data

    for pod in all_pods:
        if not isinstance(pod, dict):
            continue

        name = (
            pod.get("metadata", {}).get("name", "")
            if "metadata" in pod
            else pod.get("name", "")
        )
        namespace = (
            pod.get("metadata", {}).get("namespace", "")
            if "metadata" in pod
            else pod.get("namespace", "")
        )

        phase = (
            pod.get("status", {}).get("phase", "")
            if isinstance(pod.get("status"), dict)
            else pod.get("status", "")
        )

        container_statuses = (
            pod.get("status", {}).get("containerStatuses", [])
            if isinstance(pod.get("status"), dict)
            else []
        )

        is_problem = False
        problem_reason = phase

        if phase not in ("Running", "Succeeded"):
            is_problem = True

        for cs in container_statuses:
            if not isinstance(cs, dict):
                continue
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting:
                reason = waiting.get("reason", "")
                if reason in (
                    "CrashLoopBackOff", "Error", "ImagePullBackOff",
                    "ErrImagePull", "CreateContainerError",
                    "OOMKilled",
                ):
                    is_problem = True
                    problem_reason = reason

        if is_problem and name and namespace not in _SYSTEM_NS:
            pods.append({
                "name": name,
                "namespace": namespace,
                "status": problem_reason,
            })

    return pods[:5]


def _parse_argocd_apps(raw: Any) -> List[Dict[str, str]]:
    """Extract ArgoCD application info from MCP tool output."""
    data = _safe_json(raw)
    apps: List[Dict[str, str]] = []

    all_apps: List[Any] = []
    if isinstance(data, dict):
        raw_apps: Any = data.get("items", data.get("applications", [data]))
        all_apps = raw_apps if isinstance(raw_apps, list) else []
    elif isinstance(data, list):
        all_apps = data

    for app in all_apps:
        if not isinstance(app, dict):
            continue
        name = (
            app.get("metadata", {}).get("name", "")
            if "metadata" in app
            else app.get("name", "")
        )
        health = app.get("status", {}).get("health", {}).get("status", "")
        sync = app.get("status", {}).get("sync", {}).get("status", "")

        if name:
            apps.append({
                "name": name,
                "health": health,
                "sync": sync,
            })

    return apps


# ── Template-based suggestion builders ───────────────────────────────
# Each builder maps 1:1 to a coordinator and its sub-agent skills.
#
# Coordinator → MCP Servers → Skills:
#   Helm Operator  → helm_mcp_server, github_mcp
#       • helm-operation: install/upgrade/rollback/uninstall, release status
#       • helm-generator: new chart creation from natural language
#       • helm-validator: lint/template validation
#       • github-agent: commit charts to GitHub
#
#   K8s Operator   → kubernetes_mcp_server
#       • k8s-cluster-ops: resource CRUD, pod debugging, scaling, events,
#         logs, exec, node diagnostics, cluster health checks
#
#   App Operator   → argocd_mcp_server, argo_rollout_mcp_server, traefik_mcp_server
#       • argocd-onboarder: GitOps app lifecycle (create/sync/rollback)
#       • argo-rollouts-onboarder: progressive delivery (canary/blue-green)
#       • traefik-edge-router: weighted routing, traffic mirroring, middleware
#
#   Observability  → prometheus-mcp-server, alertmanager-mcp-server
#       • prometheus-operator: PromQL, metric exploration, exporter lifecycle,
#         ServiceMonitor, TSDB cardinality, rule authoring
#       • alertmanager-operator: alert triage, silence lifecycle, routing audit
#
#   Observability (future) → tempo_mcp_server, loki_mcp_server, otel_mcp_server
#       • tempo: distributed trace search, span analysis, trace-to-logs
#       • loki: log aggregation, LogQL queries, log-based alerting
#       • otel: collector pipeline health, instrumentation status, sampling


def _parse_prom_alerts(raw: Any) -> List[Dict[str, str]]:
    """Extract firing alert names from Prometheus ALERTS query."""
    data = _safe_json(raw)

    alerts: List[Dict[str, str]] = []

    # Prometheus instant query returns {"data": {"result": [...]}}
    if isinstance(data, dict):
        results: Any = data.get("data", {})
        if isinstance(results, dict):
            result_list: Any = results.get("result", [])
            if isinstance(result_list, list):
                for item in result_list:
                    if isinstance(item, dict):
                        metric = item.get("metric", {})
                        if isinstance(metric, dict):
                            alerts.append({
                                "name": str(metric.get("alertname", "unknown")),
                                "severity": str(metric.get("severity", "")),
                            })

    # Also handle if the raw output is a list of alert dicts
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("alertname", item.get("name", "")))
                if name:
                    alerts.append({
                        "name": name,
                        "severity": str(item.get("severity", "")),
                    })

    # Tabular text fallback
    if isinstance(data, str) and not alerts:
        rows = _parse_tabular_text(data)
        for row in rows:
            name = row.get("alertname", row.get("name", ""))
            if name:
                alerts.append({
                    "name": name,
                    "severity": row.get("severity", ""),
                })

    return alerts


def _build_helm_suggestions(
    ns_result: ProbeResult,
    release_result: ProbeResult,
) -> Dict[str, Any]:
    """
    Generate Helm Operator suggestions.

    Skills covered:
        • helm-operation (install/upgrade/rollback, release status)
        • helm-generator (new chart creation)
        • helm-validator + github-agent (validation + commit)
    """
    releases: List[Dict[str, str]] = (
        release_result.data if isinstance(release_result.data, list) else []
    )
    namespaces: List[str] = (
        ns_result.data if isinstance(ns_result.data, list) else []
    )
    prompts: List[str] = []

    if releases:
        # ── Operations skill: suggest status/upgrade for real releases ──
        r = releases[0]
        name = r.get("name", "unknown")
        ns = r.get("namespace", "default")
        prompts.append(f"Check the status of the '{name}' Helm release in {ns}")

        # Find any non-deployed releases
        non_deployed = [
            rel for rel in releases
            if rel.get("status", "deployed").lower() != "deployed"
        ]
        if non_deployed:
            nd = non_deployed[0]
            prompts.append(
                f"Investigate the '{nd.get('name')}' release "
                f"(status: {nd.get('status', 'unknown')})"
            )
        elif len(releases) > 1:
            prompts.append("List all Helm releases across the cluster")

        prompts.append(
            f"Upgrade the '{name}' release to the latest chart version"
        )
    else:
        # ── Generator skill: suggest chart creation ──
        app_ns = [n for n in namespaces if n not in _SYSTEM_NS]
        target_ns = app_ns[0] if app_ns else "default"
        prompts.append(
            "Create a production-ready Helm chart for a web application"
        )
        prompts.append(
            f"Generate a Helm chart and deploy to the '{target_ns}' namespace"
        )

    return {
        "id": "helm_operator",
        "name": "Package Management via Helm",
        "examples": prompts[:3],
    }


def _build_k8s_suggestions(
    ns_result: ProbeResult,
    pods_result: ProbeResult,
) -> Dict[str, Any]:
    """
    Generate K8s Operator suggestions.

    Skills covered:
        • k8s-cluster-ops: pod debugging, resource CRUD, scaling,
          events, logs, exec, node diagnostics, health checks
    """
    pods: List[Dict[str, str]] = (
        pods_result.data if isinstance(pods_result.data, list) else []
    )
    namespaces: List[str] = (
        ns_result.data if isinstance(ns_result.data, list) else []
    )
    prompts: List[str] = []

    # ── Debugging skill: surface real CrashLooping/failing pods ──
    if pods:
        p = pods[0]
        name = p.get("name", "unknown")
        status = p.get("status", "failing")
        ns = p.get("namespace", "default")
        prompts.append(f"Debug why the '{name}' pod is {status} in {ns}")
        if len(pods) > 1:
            prompts.append(
                f"Show all {len(pods)} pods with issues across the cluster"
            )

    # ── Cluster health + namespace awareness ──
    app_ns = [n for n in namespaces if n not in _SYSTEM_NS]
    if app_ns:
        prompts.append(
            f"Show the health overview of the '{app_ns[0]}' namespace"
        )

    # ── General cluster ops if we have room ──
    if len(prompts) < 3:
        prompts.append("Run a cluster health check across all nodes")

    return {
        "id": "k8s_operator",
        "name": "Kubernetes Cluster Operations",
        "examples": prompts[:3],
    }


def _build_app_suggestions(
    argocd_result: ProbeResult,
    rollouts_result: ProbeResult,
    traefik_result: ProbeResult,
) -> Optional[Dict[str, Any]]:
    """
    Generate App Operator suggestions.

    Skills covered:
        • argocd-onboarder: GitOps app lifecycle
        • argo-rollouts-onboarder: canary/blue-green deployments
        • traefik-edge-router: weighted routing, traffic mirroring

    Returns None to suppress when no app-layer services are reachable.
    """
    argocd_available = argocd_result.server_available
    rollouts_available = rollouts_result.server_available
    traefik_available = traefik_result.server_available

    if not argocd_available and not rollouts_available and not traefik_available:
        return None  # Suppress — no app-layer services reachable

    apps: List[Dict[str, str]] = (
        argocd_result.data if isinstance(argocd_result.data, list) else []
    )
    prompts: List[str] = []

    # ── ArgoCD skills ──
    if argocd_available:
        if apps:
            a = apps[0]
            name = a.get("name", "unknown")
            sync = a.get("sync", "")
            health = a.get("health", "")

            prompts.append(
                f"Check the sync status of the '{name}' ArgoCD application"
            )
            # Contextual suggestions based on real state
            if sync and sync.lower() in ("outofsync", "outofsynced"):
                prompts.append(
                    f"Sync the out-of-date '{name}' ArgoCD application"
                )
            if health and health.lower() in ("degraded", "missing"):
                prompts.append(
                    f"Investigate the unhealthy '{name}' ArgoCD app"
                )
            if len(apps) > 1:
                prompts.append("List all ArgoCD applications across the cluster")
        else:
            prompts.append(
                "Onboard a new ArgoCD application from a Git repository"
            )

    # ── Argo Rollouts skills (canary/blue-green) ──
    if rollouts_available and len(prompts) < 3:
        if apps:
            # Suggest progressive delivery for an existing app
            app_name = apps[0].get("name", "my-app")
            prompts.append(
                f"Set up a canary rollout strategy for '{app_name}'"
            )
        else:
            prompts.append(
                "Configure a canary deployment with Argo Rollouts"
            )

    # ── Traefik skills (if reachable and we have room) ──
    if traefik_available and len(prompts) < 3:
        prompts.append(
            "Set up weighted canary routing for a service with Traefik"
        )

    if not prompts:
        return None

    return {
        "id": "app_operator",
        "name": "Application Delivery via GitOps",
        "examples": prompts[:3],
    }


def _build_observability_suggestions(
    prom_result: ProbeResult,
    alertmanager_result: ProbeResult,
    tempo_result: ProbeResult,
    loki_result: ProbeResult,
    otel_result: ProbeResult,
) -> Optional[Dict[str, Any]]:
    """
    Generate Observability suggestions.

    Skills covered (active):
        • prometheus-operator: PromQL queries, metric exploration,
          exporter lifecycle, ServiceMonitor, TSDB cardinality, rule authoring
        • alertmanager-operator: alert triage, silence lifecycle,
          routing audit, integration testing

    Skills covered (future — placeholders):
        • tempo: distributed trace search, span analysis, trace-to-logs
        • loki: log aggregation, LogQL queries, log-based alerting
        • otel: collector pipeline health, instrumentation status, sampling

    Returns None to suppress when no observability services are reachable.
    """
    prom_available = prom_result.server_available
    am_available = alertmanager_result.server_available
    tempo_available = tempo_result.server_available
    loki_available = loki_result.server_available
    otel_available = otel_result.server_available

    if not any([prom_available, am_available, tempo_available,
                loki_available, otel_available]):
        return None  # Suppress — no observability services reachable

    prompts: List[str] = []

    # ── Prometheus skills ──
    if prom_available:
        prom_data = prom_result.data if isinstance(prom_result.data, dict) else {}
        alerts: List[Dict[str, str]] = prom_data.get("alerts", [])

        if alerts:
            # Surface real firing alerts
            alert = alerts[0]
            alert_name = alert.get("name", "unknown")
            severity = alert.get("severity", "")
            severity_tag = f" (severity: {severity})" if severity else ""
            prompts.append(
                f"What's firing? Investigate the '{alert_name}' alert{severity_tag}"
            )
            if len(alerts) > 1:
                prompts.append(
                    f"Triage all {len(alerts)} firing alerts across the cluster"
                )
        else:
            # No alerts — suggest PromQL exploration
            prompts.append(
                "Query the current CPU and memory usage across all namespaces"
            )

        # ── Exporter / monitoring onboarding ──
        if len(prompts) < 3:
            prompts.append(
                "Check TSDB cardinality and identify high-cardinality metrics"
            )

    # ── AlertManager skills ──
    if am_available and len(prompts) < 3:
        prompts.append(
            "Show on-call alert summary and active silences"
        )

    # ── Tempo skills (distributed tracing) ──
    if tempo_available:
        tempo_data = tempo_result.data if isinstance(tempo_result.data, dict) else {}
        backends = tempo_data.get("backends", [])
        if backends and len(prompts) < 3:
            backend = backends[0] if isinstance(backends[0], dict) else {}
            backend_id = backend.get("id", backend.get("backend_id", os.environ.get("TEMPO_BACKEND_ID", "default")))
            prompts.append(
                f"Search traces for high-latency requests in Tempo backend '{backend_id}'"
            )
        elif len(prompts) < 3:
            prompts.append("List and inspect all configured Tempo tracing backends")

    # ── Loki skills (log aggregation) ──
    if loki_available:
        loki_data = loki_result.data if isinstance(loki_result.data, dict) else {}
        labels = loki_data.get("labels", [])
        if labels and len(prompts) < 3:
            lbl = labels[0] if labels else "app"
            prompts.append(
                f"Query recent error logs using the '{lbl}' label with LogQL"
            )
        elif len(prompts) < 3:
            prompts.append("Discover available log labels in the Loki cluster")

    # ── OpenTelemetry skills (collector pipeline) ──
    if otel_available:
        otel_data = otel_result.data if isinstance(otel_result.data, dict) else {}
        collectors = otel_data.get("collectors", [])
        if collectors and len(prompts) < 3:
            c = collectors[0] if isinstance(collectors[0], dict) else {}
            c_name = c.get("metadata", {}).get("name", c.get("name", "unknown"))
            ns = c.get("metadata", {}).get("namespace", c.get("namespace", os.environ.get("K8S_DEFAULT_NAMESPACE", "default")))
            prompts.append(
                f"Investigate the OpenTelemetry collector pipeline for '{c_name}' in '{ns}'"
            )
        elif len(prompts) < 3:
            prompts.append("Provision a new OpenTelemetry collector with trace and metric pipelines")

    if not prompts:
        return None

    return {
        "id": "observability",
        "name": "Cluster Observability & Alerting",
        "examples": prompts[:3],
    }


# ── Utility ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

