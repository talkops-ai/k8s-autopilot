"""
Helm Operations Context — Journal tool and helpers for context persistence.

Solves the classic deep-agent context loss problem: after the built-in
summarization compresses conversation history (~85% of context window),
critical operational details (chart URLs, repos, values used) are lost.

Architecture (3-layer context engineering):
    Layer 1 — This module: ``log_helm_operation`` tool writes a structured
              entry to ``/memories/helm-operator/operations-log.md`` via
              the virtual filesystem.  Since ``/memories/`` routes to
              ``StoreBackend``, entries persist across threads.

    Layer 2 — ``OperationContextMiddleware`` (middleware.py): ``before_model``
              hook reads the journal and re-injects it as a SystemMessage
              before every coordinator model call, surviving summarization.

    Layer 3 — Prompt engineering (coordinator, subagent, SKILL.md, AGENTS.md)
              tells the LLM to always reference the journal for follow-ups.

Reference:
    LangChain docs — Context Engineering → Long-term memory
    https://docs.langchain.com/oss/python/deepagents/context-engineering
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain.tools import tool, ToolRuntime
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("OperationsContext")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPERATIONS_LOG_PATH = "/memories/helm-operator/operations-log.md"
APP_OPERATIONS_LOG_PATH = "/memories/app-operator/operations-log.md"
K8S_OPERATIONS_LOG_PATH = "/memories/k8s-operator/operations-log.md"
OBS_OPERATIONS_LOG_PATH = "/memories/observability/operations-log.md"

# Maximum entries kept in the journal to prevent unbounded growth.
_MAX_JOURNAL_ENTRIES = 20

# ---------------------------------------------------------------------------
# Internal Helper: Write to journal
# ---------------------------------------------------------------------------

def _write_to_journal(
    runtime: ToolRuntime,
    log_path: str,
    action: str,
    entry_details: dict[str, str],
    journal_title: str,
    config: Optional[RunnableConfig] = None,
) -> Command:
    """Generic helper to write a log entry to a specified virtual filesystem path."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    entry = f"\n### {action.upper()} ({timestamp})\n"
    for key, value in entry_details.items():
        if value:
            entry += f"- **{key}**: `{value}`\n"

    # ── Write to virtual filesystem via state ────────────────────────
    existing_log = ""
    if hasattr(runtime, "state") and isinstance(runtime.state, dict):
        files = runtime.state.get("files", {})
        log_file = files.get(log_path)
        if log_file:
            if isinstance(log_file, dict):
                existing_log = log_file.get("content", "")
            elif isinstance(log_file, str):
                existing_log = log_file
            else:
                existing_log = getattr(log_file, "content", "")

    if not existing_log:
        existing_log = (
            f"# {journal_title}\n\n"
            "Auto-generated log of operations performed in this "
            "session. Used by the coordinator to maintain context across "
            "conversation turns and after summarization.\n"
        )

    # Append the new entry
    updated_log = existing_log.rstrip() + "\n" + entry

    # ── Trim to max entries ───────────────────────────────────────────
    lines = updated_log.split("\n### ")
    if len(lines) > _MAX_JOURNAL_ENTRIES + 1:  # +1 for header
        header = lines[0]
        kept = lines[-(_MAX_JOURNAL_ENTRIES):]
        updated_log = header + "\n### " + "\n### ".join(kept)

    from deepagents.backends.utils import create_file_data

    summary_str = " ".join([f"{v}" for k, v in list(entry_details.items())[:3] if v])
    file_data = create_file_data(updated_log)

    if config is not None:
        store = config.get("store")
        if store is not None:
            org_name = "default_org"
            if hasattr(runtime, "state") and isinstance(runtime.state, dict):
                ctx = runtime.state.get("context", {})
                if isinstance(ctx, dict):
                    org_name = ctx.get("org_name", "default_org")
            
            key = log_path.replace("/memories/", "").lstrip("/")
            store.put((org_name,), key, dict(file_data))
            logger.info(f"Synchronized operations journal to StoreBackend | org={org_name} key={key}")

    return Command(update={
        "messages": [
            ToolMessage(
                content=(
                    f"✅ Operation logged: {action} {summary_str}. "
                    f"Context will be preserved for follow-up operations."
                ),
                tool_call_id=runtime.tool_call_id,
            ),
        ],
        "files": {
            log_path: file_data,
        },
    })


# ---------------------------------------------------------------------------
# Tool: log_helm_operation
# ---------------------------------------------------------------------------

def create_log_operation_tool() -> Any:
    """Factory that returns the ``log_helm_operation`` coordinator tool.

    Called by the coordinator after every successful helm-operation task
    to persist critical context (chart source, release, values) so that
    follow-up operations never need to re-ask the user.
    """

    @tool
    def log_helm_operation(
        action: str,
        release_name: str,
        namespace: str,
        chart_source: str,
        runtime: ToolRuntime,
        config: RunnableConfig,
        values: Optional[str] = None,
        version: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Command:
        """Log a completed helm operation to the persistent operations journal.

        MUST be called after every successful state-modifying helm operation
        (install, upgrade, rollback, uninstall).  This ensures follow-up
        operations have full context even after conversation summarization.

        Args:
            action: Operation type — install, upgrade, rollback, or uninstall.
            release_name: Helm release name (e.g. "web-release").
            namespace: Target Kubernetes namespace.
            chart_source: Full chart reference — repo/chart (e.g. "bitnami/nginx"),
                          OCI URL (e.g. "oci://registry/chart"), or local path.
            values: Key configuration values as YAML or key=value.
                    e.g. "replicaCount=2, service.type=LoadBalancer"
            version: Chart version installed/upgraded to.
            notes: Any additional context (e.g. "user requested via Helm URL").
        """
        entry_details = {
            "Release": release_name,
            "Namespace": namespace,
            "Chart Source": chart_source,
            "Version": version,
            "Values": values,
            "Notes": notes,
        }

        logger.info(
            "Logged helm operation to journal",
            extra={
                "action": action,
                "release_name": release_name,
                "namespace": namespace,
                "chart_source": chart_source,
            },
        )

        return _write_to_journal(
            runtime=runtime,
            log_path=OPERATIONS_LOG_PATH,
            action=action,
            entry_details=entry_details,
            journal_title="Helm Operations Journal",
            config=config,
        )

    return log_helm_operation

# ---------------------------------------------------------------------------
# Tool: log_app_operation
# ---------------------------------------------------------------------------

def create_log_app_operation_tool() -> Any:
    """Factory that returns the ``log_app_operation`` coordinator tool."""

    @tool
    def log_app_operation(
        action: str,
        app_name: str,
        namespace: str,
        runtime: ToolRuntime,
        config: RunnableConfig,
        repo_url: Optional[str] = None,
        project: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Command:
        """Log a completed App Operator / GitOps operation to the persistent journal.

        MUST be called after every successful state-modifying app operation
        (create_application, sync, delete). This ensures follow-up
        operations have full context.

        Args:
            action: Operation type — create, sync, delete, update.
            app_name: Application name.
            namespace: Target namespace.
            repo_url: Source Git repository URL.
            project: ArgoCD project name.
            notes: Additional context.
        """
        entry_details = {
            "Application": app_name,
            "Namespace": namespace,
            "Repo URL": repo_url,
            "Project": project,
            "Notes": notes,
        }

        logger.info(
            "Logged app operation to journal",
            extra={
                "action": action,
                "app_name": app_name,
                "namespace": namespace,
            },
        )

        return _write_to_journal(
            runtime=runtime,
            log_path=APP_OPERATIONS_LOG_PATH,
            action=action,
            entry_details=entry_details,
            journal_title="App Operations Journal",
            config=config,
        )

    return log_app_operation


# ---------------------------------------------------------------------------
# Helper: extract operations context from state
# ---------------------------------------------------------------------------

def _get_context_from_state(
    state: Dict[str, Any],
    log_path: str,
    domain_name: str,
) -> Optional[str]:
    """Helper to read the operations journal from agent state and return compact context."""
    files = state.get("files", {})
    log_file = files.get(log_path)
    if not log_file:
        return None

    if isinstance(log_file, dict):
        content = log_file.get("content", "")
    elif isinstance(log_file, str):
        content = log_file
    else:
        content = getattr(log_file, "content", "")

    if not content or content.strip() == "":
        return None

    entries: List[str] = []
    for block in content.split("\n### "):
        block = block.strip()
        if block and not block.startswith(f"# {domain_name} Operations"):
            entries.append(f"### {block}")

    if not entries:
        return None

    recent = entries[-5:]
    return (
        f"## Recent {domain_name} Operations (from journal)\n"
        f"Use this context for follow-up requests. Do NOT re-ask the user "
        f"for details already listed here.\n\n"
        + "\n".join(recent)
    )


def get_operations_context_from_state(
    state: Dict[str, Any],
) -> Optional[str]:
    """Read the Helm operations journal from agent state and return compact context."""
    return _get_context_from_state(state, OPERATIONS_LOG_PATH, "Helm")


def get_app_operations_context_from_state(
    state: Dict[str, Any],
) -> Optional[str]:
    """Read the App operations journal from agent state and return compact context."""
    return _get_context_from_state(state, APP_OPERATIONS_LOG_PATH, "App")


def get_k8s_operations_context_from_state(
    state: Dict[str, Any],
) -> Optional[str]:
    """Read the K8s operations journal from agent state and return compact context."""
    return _get_context_from_state(state, K8S_OPERATIONS_LOG_PATH, "K8s")


def get_obs_operations_context_from_state(
    state: Dict[str, Any],
) -> Optional[str]:
    """Read the Observability operations journal from agent state and return compact context."""
    return _get_context_from_state(state, OBS_OPERATIONS_LOG_PATH, "Observability")


# ---------------------------------------------------------------------------
# Tool: log_k8s_operation
# ---------------------------------------------------------------------------

def create_log_k8s_operation_tool() -> Any:
    """Factory that returns the ``log_k8s_operation`` coordinator tool.

    Called by the K8s Operator coordinator after every successful
    state-modifying cluster operation to persist context (resource kind,
    name, namespace, action) so that follow-up operations never need to
    re-ask the user.
    """

    @tool
    def log_k8s_operation(
        action: str,
        resource_kind: str,
        resource_name: str,
        namespace: str,
        runtime: ToolRuntime,
        config: RunnableConfig,
        api_version: Optional[str] = None,
        cluster_context: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Command:
        """Log a completed K8s cluster operation to the persistent operations journal.

        MUST be called after every successful state-modifying K8s operation
        (create, update, scale, delete, exec, run). This ensures follow-up
        operations have full context even after conversation summarization.

        Args:
            action: Operation type — create, update, scale, delete, exec, run.
            resource_kind: Kubernetes resource kind (e.g. "Deployment", "Pod").
            resource_name: Name of the resource.
            namespace: Target Kubernetes namespace.
            api_version: Kubernetes apiVersion (e.g. "apps/v1").
            cluster_context: Kubeconfig context name (for multi-cluster).
            notes: Additional context (e.g. "scaled from 3 to 5 replicas").
        """
        entry_details = {
            "Kind": resource_kind,
            "Name": resource_name,
            "Namespace": namespace,
            "apiVersion": api_version,
            "Context": cluster_context,
            "Notes": notes,
        }

        logger.info(
            "Logged K8s operation to journal",
            extra={
                "action": action,
                "resource_kind": resource_kind,
                "resource_name": resource_name,
                "namespace": namespace,
            },
        )

        return _write_to_journal(
            runtime=runtime,
            log_path=K8S_OPERATIONS_LOG_PATH,
            action=action,
            entry_details=entry_details,
            journal_title="K8s Operations Journal",
            config=config,
        )

    return log_k8s_operation


# ---------------------------------------------------------------------------
# Tool: log_obs_operation
# ---------------------------------------------------------------------------

def create_log_obs_operation_tool() -> Any:
    """Factory that returns the ``log_obs_operation`` coordinator tool.

    Called by the Observability coordinator after every successful
    state-modifying observability operation to persist context
    (target system, operation type, resource name, backend) so that
    follow-up operations never need to re-ask the user.
    """

    @tool
    def log_obs_operation(
        action: str,
        target_system: str,
        operation_type: str,
        resource_name: str,
        runtime: ToolRuntime,
        config: RunnableConfig,
        backend_id: Optional[str] = None,
        namespace: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Command:
        """Log a completed Observability operation to the persistent operations journal.

        MUST be called after every successful state-modifying observability
        operation (exporter install/uninstall, rule creation, silence
        create/expire, ServiceMonitor apply). This ensures follow-up
        operations have full context even after conversation summarization.

        Args:
            action: Operation type — install, uninstall, create, delete,
                    upsert, expire, apply, configure.
            target_system: "prometheus" or "alertmanager".
            operation_type: Category — query, rule, silence, exporter,
                            config, onboarding, triage.
            resource_name: Name of the affected resource (e.g. exporter
                           name, rule group, silence ID, ServiceMonitor).
            backend_id: Prometheus/Alertmanager backend ID.
            namespace: Target Kubernetes namespace (if applicable).
            notes: Additional context.
        """
        entry_details = {
            "Target System": target_system,
            "Operation": operation_type,
            "Resource": resource_name,
            "Backend": backend_id,
            "Namespace": namespace,
            "Notes": notes,
        }

        logger.info(
            "Logged observability operation to journal",
            extra={
                "action": action,
                "target_system": target_system,
                "operation_type": operation_type,
                "resource_name": resource_name,
            },
        )

        return _write_to_journal(
            runtime=runtime,
            log_path=OBS_OPERATIONS_LOG_PATH,
            action=action,
            entry_details=entry_details,
            journal_title="Observability Operations Journal",
            config=config,
        )

    return log_obs_operation
