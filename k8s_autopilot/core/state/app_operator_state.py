from typing import Annotated, Sequence, Dict, Any, Optional
from typing_extensions import NotRequired, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

class AppOperatorContext(TypedDict, total=False):
    messages: Annotated[Sequence[AnyMessage], add_messages]
    agent_messages: Annotated[Sequence[AnyMessage], add_messages]
    pending_interrupt: Optional[Dict[str, Any]]
    collected_inputs: Dict[str, Any]
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
    
    # ── Cross-domain routing ──────────────────────────────────────────
    escalation_request: NotRequired[dict]
    handoff_request: NotRequired[dict]
