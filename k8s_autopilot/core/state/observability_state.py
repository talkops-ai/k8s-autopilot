from typing import Dict
from typing_extensions import NotRequired, TypedDict


class ObservabilityContext(TypedDict, total=False):
    """Runtime context for the Observability Deep Agent.

    Injected via ``config["context"]`` in LangGraph invocation.
    All fields are optional — the coordinator populates them from
    environment variables and supervisor state at invocation time.

    The context propagates automatically to all subagents via
    ``ToolRuntime``, enabling subagents to filter queries by
    service, environment, and incident without re-explanation.
    """

    # ── Backend connectivity ──────────────────────────────────────────
    prometheus_url: NotRequired[str]
    alertmanager_url: NotRequired[str]
    default_backend_id: NotRequired[str]

    # ── Kubernetes context ────────────────────────────────────────────
    cluster_context: NotRequired[str]
    kubeconfig_path: NotRequired[str]
    default_namespace: NotRequired[str]

    # ── SRE investigation context ─────────────────────────────────────
    # These fields carry investigation-scoped state across subagent
    # delegations so each subagent can auto-scope its queries.
    service_name: NotRequired[str]       # e.g. "checkout", "payments"
    environment: NotRequired[str]        # e.g. "prod", "staging", "dev"
    tenant_id: NotRequired[str]          # multi-tenant isolation key
    time_window: NotRequired[str]        # e.g. "last_15m", "last_1h", ISO range
    incident_id: NotRequired[str]        # links to an active incident
    user_id: NotRequired[str]            # identity of the requesting user
    additional_labels: NotRequired[Dict[str, str]]  # extra K/V label filters

    # ── Session tracking ──────────────────────────────────────────────
    session_id: NotRequired[str]
    task_id: NotRequired[str]

    # ── Safety ────────────────────────────────────────────────────────
    dry_run: NotRequired[bool]
