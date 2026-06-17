from typing import Optional
from typing_extensions import NotRequired, TypedDict

class HelmOperatorContext(TypedDict, total=False):
    github_repo: NotRequired[str]
    github_branch: NotRequired[str]
    github_commit_author: NotRequired[str]
    workspace_path: NotRequired[str]
    service: NotRequired[str]
    cluster_context: NotRequired[str]
    kubeconfig_path: NotRequired[str]
    default_namespace: NotRequired[str]
    workflow_mode: NotRequired[str]
    require_approval: NotRequired[bool]
    session_id: NotRequired[str]
    task_id: NotRequired[str]
    dry_run: NotRequired[bool]
    
    # ── Cross-domain routing ──────────────────────────────────────────
    escalation_request: NotRequired[dict]
    handoff_request: NotRequired[dict]