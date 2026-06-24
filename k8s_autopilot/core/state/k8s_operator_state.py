from typing_extensions import NotRequired, TypedDict


class K8sOperatorContext(TypedDict, total=False):
    """Runtime context for the K8s Operator Deep Agent.

    Injected via ``config["context"]`` during LangGraph invocation.
    """
    cluster_context: NotRequired[str]
    kubeconfig_path: NotRequired[str]
    default_namespace: NotRequired[str]
    workspace_path: NotRequired[str]
    read_only: NotRequired[bool]
    disable_destructive: NotRequired[bool]
    dry_run: NotRequired[bool]
    require_approval: NotRequired[bool]
    session_id: NotRequired[str]
    task_id: NotRequired[str]

    # ── Cross-domain routing ──────────────────────────────────────────
    escalation_request: NotRequired[dict]
    handoff_request: NotRequired[dict]
