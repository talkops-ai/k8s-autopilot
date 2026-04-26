"""
K8s Operator — Modular middleware factory for deep agent safety nets.

Provides HITL approval gates for destructive and high-risk Kubernetes
cluster operations, plus configurable tool/model call limits.

Follows the same DRY ``_make_interrupt_config`` pattern established
in the App Operator middleware.

Usage::

    from k8s_autopilot.core.agents.k8s_operator.middleware import (
        build_k8s_operator_middleware,
        build_k8s_cluster_ops_hitl_middleware,
    )
"""
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from langchain.agents.middleware import HumanInTheLoopMiddleware, AgentMiddleware, AgentState
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
from k8s_autopilot.core.hitl.safe_resume import SafeResumeHITLMiddleware
from langchain_core.messages import SystemMessage
from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("K8sOperatorMiddleware")

# ---------------------------------------------------------------------------
# Default limits (overridable via env vars)
# ---------------------------------------------------------------------------

_WRITE_FILE_RUN_LIMIT = int(os.getenv("K8S_OP_WRITE_FILE_RUN_LIMIT", "20"))
_GLOBAL_TOOL_RUN_LIMIT = int(os.getenv("K8S_OP_GLOBAL_TOOL_RUN_LIMIT", "60"))
_MODEL_CALL_RUN_LIMIT = int(os.getenv("K8S_OP_MODEL_CALL_RUN_LIMIT", "40"))
_ENABLE_TOOL_RETRY = os.getenv("K8S_OP_ENABLE_TOOL_RETRY", "false").lower() == "true"

# Production namespace patterns — elevated warnings
_PRODUCTION_NAMESPACES = {"production", "prod", "live", "prd"}
_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


# ---------------------------------------------------------------------------
# Layer 2: K8sOperationContextMiddleware — survives summarization
# ---------------------------------------------------------------------------

class K8sOperationContextMiddleware(AgentMiddleware):
    """Injects recent K8s operations context before every model call.

    Reads ``/memories/k8s-operator/operations-log.md`` from the agent's
    ``state["files"]`` and prepends a compact SystemMessage with recent
    operation details. This context survives built-in summarization.
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Read operations journal and inject as SystemMessage."""
        from k8s_autopilot.utils.operations_context import (
            get_k8s_operations_context_from_state,
        )

        ops_context = get_k8s_operations_context_from_state(dict(state))
        if not ops_context:
            return None

        logger.debug(
            "K8sOperationContextMiddleware: injecting operations context",
            extra={"context_length": len(ops_context)},
        )

        return {
            "messages": [
                SystemMessage(
                    content=(
                        "## Active K8s Operations Context (auto-injected, "
                        "survives summarization)\n"
                        "The following operations were performed in this "
                        "session. Use this context for ANY follow-up "
                        "requests. NEVER re-ask the user for details that are "
                        "listed here.\n\n"
                        f"{ops_context}"
                    )
                )
            ],
        }

    async def abefore_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Async version — delegates to sync."""
        return self.before_model(state, runtime)


def _is_production_namespace(namespace: str) -> bool:
    """Check if a namespace matches known production patterns."""
    return str(namespace or "").strip().lower() in _PRODUCTION_NAMESPACES


def _is_system_namespace(namespace: str) -> bool:
    """Check if a namespace matches Kubernetes system namespaces."""
    return str(namespace or "").strip().lower() in _SYSTEM_NAMESPACES


# ---------------------------------------------------------------------------
# DRY helper — reused from App Operator pattern
# ---------------------------------------------------------------------------

def _make_interrupt_config(
    tool_name: str,
    description_builder: Any,
    *,
    allowed_decisions: Any = None,
) -> InterruptOnConfig:
    """Create an ``InterruptOnConfig`` for a tool with a description builder."""
    return InterruptOnConfig(
        allowed_decisions=allowed_decisions or ["approve", "reject"],
        description=lambda tool_call, state, runtime, _tn=tool_name, _db=description_builder: (
            _db(_tn, tool_call.get("args", {}))
        ),
    )


# ---------------------------------------------------------------------------
# Kubernetes Cluster Ops — approval card descriptions
# ---------------------------------------------------------------------------

def _build_k8s_approval_description(
    tool_name: str, tool_args: Dict[str, Any]
) -> str:
    """Build a rich, human-readable description for K8s cluster ops HITL cards.

    Covers resource deletion, creation/update (upsert), scaling, pod exec,
    pod deletion, and pod run operations per SKILL.md safety rules.
    """

    ns = tool_args.get("namespace", "default")
    name = tool_args.get("name", "unknown")
    kind = tool_args.get("kind", "")
    api_version = tool_args.get("apiVersion", tool_args.get("api_version", ""))

    is_prod = _is_production_namespace(ns)
    is_sys = _is_system_namespace(ns)
    env_badge = ""
    if is_prod:
        env_badge = "\n\n🚨 **TARGET: PRODUCTION NAMESPACE**"
    elif is_sys:
        env_badge = "\n\n🚨 **TARGET: SYSTEM NAMESPACE** — Caution: system-critical resources"

    # ── resources_delete ──────────────────────────────────────────────────
    if tool_name == "resources_delete":
        grace = tool_args.get("gracePeriodSeconds", None)
        force_note = ""
        if grace is not None and int(grace) == 0:
            force_note = "\n⚡ **FORCE DELETE** — gracePeriodSeconds=0, bypasses graceful shutdown"
        return (
            f"🗑️ **DELETE RESOURCE — APPROVAL REQUIRED**\n\n"
            f"**Kind**: {kind}\n"
            f"**Name**: {name}\n"
            f"**Namespace**: {ns}\n"
            f"**apiVersion**: {api_version}"
            f"{force_note}"
            f"{env_badge}\n\n"
            f"⚠️ This permanently removes the resource from the cluster."
        )

    # ── pods_delete ───────────────────────────────────────────────────────
    elif tool_name == "pods_delete":
        return (
            f"🗑️ **DELETE POD — APPROVAL REQUIRED**\n\n"
            f"**Pod**: {name}\n"
            f"**Namespace**: {ns}"
            f"{env_badge}\n\n"
            f"⚠️ Pod will be terminated. If managed by a controller "
            f"(Deployment/ReplicaSet), a replacement will be scheduled."
        )

    # ── resources_create_or_update ────────────────────────────────────────
    elif tool_name == "resources_create_or_update":
        resource_yaml = tool_args.get("resource", "")
        # Try to extract kind/name from the YAML string
        yaml_preview = resource_yaml[:300] + "..." if len(resource_yaml) > 300 else resource_yaml
        return (
            f"📝 **CREATE/UPDATE RESOURCE — APPROVAL REQUIRED**\n\n"
            f"**Operation**: Upsert (creates if absent, updates if exists)\n"
            f"**Namespace**: {ns}"
            f"{env_badge}\n\n"
            f"```yaml\n{yaml_preview}\n```\n\n"
            f"⚠️ This is a server-side apply. Existing resources with matching "
            f"name+namespace will be overwritten."
        )

    # ── resources_scale ──────────────────────────────────────────────────
    elif tool_name == "resources_scale":
        scale_to = tool_args.get("scale", None)
        if scale_to is None:
            return (
                f"📊 **SCALE READ — No approval needed**\n\n"
                f"**Kind**: {kind} / **Name**: {name}\n"
                f"**Namespace**: {ns}\n"
                f"Read-only: checking current replica count."
            )
        scale_warning = ""
        if int(scale_to) == 0:
            scale_warning = "\n\n🚨 **SCALING TO ZERO** — All pods will be terminated. Service will be unavailable."
        return (
            f"⚖️ **SCALE RESOURCE — APPROVAL REQUIRED**\n\n"
            f"**Kind**: {kind}\n"
            f"**Name**: {name}\n"
            f"**Namespace**: {ns}\n"
            f"**Target Replicas**: {scale_to}"
            f"{scale_warning}"
            f"{env_badge}"
        )

    # ── pods_exec ─────────────────────────────────────────────────────────
    elif tool_name == "pods_exec":
        command = tool_args.get("command", [])
        container = tool_args.get("container", "default")
        cmd_str = " ".join(command) if isinstance(command, list) else str(command)
        return (
            f"🔐 **POD EXEC — APPROVAL REQUIRED**\n\n"
            f"**Pod**: {name}\n"
            f"**Namespace**: {ns}\n"
            f"**Container**: {container}\n"
            f"**Command**: `{cmd_str}`"
            f"{env_badge}\n\n"
            f"⚠️ This grants shell-level access to the container. "
            f"Treat as equivalent to SSH access."
        )

    # ── pods_run ──────────────────────────────────────────────────────────
    elif tool_name == "pods_run":
        image = tool_args.get("image", "unknown")
        pod_name = tool_args.get("name", "auto-generated")
        port = tool_args.get("port", None)
        return (
            f"🚀 **RUN POD — APPROVAL REQUIRED**\n\n"
            f"**Image**: {image}\n"
            f"**Name**: {pod_name}\n"
            f"**Namespace**: {ns}"
            f"{'' if not port else f'  |  **Port**: {port}'}"
            f"{env_badge}\n\n"
            f"⚠️ This creates a real pod consuming cluster resources. "
            f"Remember to clean up after use."
        )

    else:
        return f"Approval required for Kubernetes operation: {tool_name}."


# ---------------------------------------------------------------------------
# Middleware factory — Kubernetes cluster ops
# ---------------------------------------------------------------------------

def build_k8s_cluster_ops_hitl_middleware() -> SafeResumeHITLMiddleware:
    """Create a ``SafeResumeHITLMiddleware`` for Kubernetes cluster operations.

    Gated tools (from SKILL.md safety rules #1–#8):
        - resources_delete — destructive, irreversible
        - pods_delete — pod termination
        - resources_create_or_update — upsert, can overwrite existing resources
        - resources_scale — replica changes, includes scale-to-zero
        - pods_exec — shell access to containers
        - pods_run — creates real pods consuming resources
    """
    logger.info("Building SafeResumeHITLMiddleware for K8s cluster ops tools")

    return SafeResumeHITLMiddleware(
        interrupt_on={
            "resources_delete": _make_interrupt_config(
                "resources_delete", _build_k8s_approval_description,
            ),
            "pods_delete": _make_interrupt_config(
                "pods_delete", _build_k8s_approval_description,
            ),
            "resources_create_or_update": _make_interrupt_config(
                "resources_create_or_update", _build_k8s_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "resources_scale": _make_interrupt_config(
                "resources_scale", _build_k8s_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "pods_exec": _make_interrupt_config(
                "pods_exec", _build_k8s_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
            "pods_run": _make_interrupt_config(
                "pods_run", _build_k8s_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
        },
        description_prefix="⚠️ Kubernetes Cluster Operation — Approval Required",
    )


# ---------------------------------------------------------------------------
# Coordinator-level middleware stack
# ---------------------------------------------------------------------------

def build_k8s_operator_middleware(
    config: Optional["Config"] = None,
    *,
    write_file_limit: Optional[int] = None,
    global_tool_limit: Optional[int] = None,
    model_call_limit: Optional[int] = None,
    enable_tool_retry: Optional[bool] = None,
    extra_middleware: Optional[List[Any]] = None,
) -> List[Any]:
    """Assemble the middleware stack for the K8sOperatorCoordinator deep agent."""
    from langchain.agents.middleware import (
        ToolCallLimitMiddleware,
        ModelCallLimitMiddleware,
        ToolRetryMiddleware,
    )

    middleware: List[Any] = []

    # 0. Operations context injection (Layer 2 — survives summarization)
    middleware.append(K8sOperationContextMiddleware())

    # 1. Per-tool write_file guard
    wf_limit = write_file_limit or _WRITE_FILE_RUN_LIMIT
    middleware.append(
        ToolCallLimitMiddleware(
            tool_name="write_file",
            run_limit=wf_limit,
            exit_behavior="end",
        )
    )

    # 2. Global tool call guard
    gt_limit = global_tool_limit or _GLOBAL_TOOL_RUN_LIMIT
    middleware.append(
        ToolCallLimitMiddleware(
            run_limit=gt_limit,
            exit_behavior="end",
        )
    )

    # 3. Model call guard
    mc_limit = model_call_limit or _MODEL_CALL_RUN_LIMIT
    middleware.append(
        ModelCallLimitMiddleware(
            run_limit=mc_limit,
            exit_behavior="end",
        )
    )

    # 4. Tool retry (transient failures)
    should_retry = enable_tool_retry if enable_tool_retry is not None else _ENABLE_TOOL_RETRY
    if should_retry:
        middleware.append(
            ToolRetryMiddleware(
                max_retries=2,
                backoff_factor=1.5,
                initial_delay=0.5,
                max_delay=10.0,
                on_failure="continue",
            )
        )

    # 5. Extra middleware
    if extra_middleware:
        middleware.extend(extra_middleware)

    return middleware
