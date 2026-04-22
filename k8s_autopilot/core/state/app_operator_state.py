from typing_extensions import NotRequired, TypedDict

class AppOperatorContext(TypedDict, total=False):
    """Runtime context for the App Operator Deep Agent."""
    argocd_server: NotRequired[str]
    github_repo: NotRequired[str]
    github_branch: NotRequired[str]
    workspace_path: NotRequired[str]
    cluster_context: NotRequired[str]
    kubeconfig_path: NotRequired[str]
    default_namespace: NotRequired[str]
    require_approval: NotRequired[bool]
    session_id: NotRequired[str]
    task_id: NotRequired[str]
    dry_run: NotRequired[bool]
