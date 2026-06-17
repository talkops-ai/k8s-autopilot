"""
App Operator — Modular middleware factory for deep agent safety nets.

Provides a configurable factory that assembles middleware stacks for the
AppOperatorCoordinator deep agent. Each middleware is independently
toggleable via ``Config`` or environment variables.

Usage::

    from k8s_autopilot.core.agents.app_operator.middleware import (
        build_app_operator_middleware,
    )

    middleware = build_app_operator_middleware(config)
    agent = create_deep_agent(
        ...,
        middleware=middleware,
    )
"""
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from deepagents.middleware.summarization import create_summarization_tool_middleware  # noqa: F401 — re-exported
from langchain.agents.middleware import HumanInTheLoopMiddleware, AgentMiddleware, AgentState
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

from langchain_core.messages import SystemMessage
from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("AppOperatorMiddleware")


# ---------------------------------------------------------------------------
# Layer 2: AppOperationContextMiddleware — survives summarization
# ---------------------------------------------------------------------------

class AppOperationContextMiddleware(AgentMiddleware):
    """Injects recent app operations context before every model call.

    Reads ``/memories/app-operator/operations-log.md`` from the agent's
    ``state["files"]`` and prepends a compact SystemMessage with recent
    operation details. This context survives built-in summarization.
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Read operations journal and inject as SystemMessage."""
        from k8s_autopilot.utils.operations_context import (
            get_app_operations_context_from_state,
        )

        ops_context = get_app_operations_context_from_state(dict(state))
        if not ops_context:
            return None

        logger.debug(
            "AppOperationContextMiddleware: injecting operations context",
            extra={"context_length": len(ops_context)},
        )

        return {
            "messages": [
                SystemMessage(
                    content=(
                        "## Active Operations Context (auto-injected, "
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
        return self.before_model(state, runtime)


# ---------------------------------------------------------------------------
# Layer 3: PlanLockMiddleware — enforces plan→execute fidelity
# ---------------------------------------------------------------------------

class PlanLockMiddleware(AgentMiddleware):
    """Re-injects the approved plan as a binding constraint before every model call.

    Industry pattern: Terraform plan/apply — the plan file constrains what
    ``apply`` can do.  OPA/Gatekeeper — admission controller rejects
    mutations that violate policy.

    This middleware reads from **two sources** (checked in order):

    1. **``state["todos"]``** — the native Deep Agent ``TodoListMiddleware``
       channel.  When there are non-completed todos, the middleware serialises 
       them as a SystemMessage constraint.  This is the Deep Agent-idiomatic approach.

    2. **``state["files"]["/plan/active-plan.md"]``** — legacy fallback.  If
       a coordinator writes a plan to this path via ``write_file``, the
       middleware picks it up.

    Lifecycle:
        1. Coordinator classifies request → PATH A (complex) or PATH B (simple).
        2. PATH A: Coordinator calls ``write_todos`` → presents plan →
           calls ``request_user_input`` for approval.
        3. User approves → execution continues.
        4. ``before_model``: when non-completed todos exist, re-injects 
           them as a binding SystemMessage.
        5. When all todos are ``completed``, injects a walkthrough-generation
           instruction instead.

    Reference:
        - https://docs.langchain.com/oss/python/langchain/middleware/built-in#to-do-list
    """

    _PLAN_PATH = "/plan/active-plan.md"

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Re-inject approved plan as a binding constraint.

        Checks ``state["todos"]`` first (Deep Agent native), then falls
        back to ``state["files"]`` (legacy).
        """
        # ── Source 1: Deep Agent TodoListMiddleware state ──────────────
        todos = state.get("todos") or []

        if isinstance(todos, list) and todos:
            return self._build_todos_constraint(todos, state)

        # ── Source 2: Legacy files-based plan (backward compat) ────────
        plan_content = self._get_active_plan_from_files(state)
        if plan_content:
            logger.debug(
                "PlanLockMiddleware: injecting legacy file-based plan",
                extra={"plan_length": len(plan_content)},
            )
            return {
                "messages": [
                    SystemMessage(
                        content=(
                            "## ACTIVE PLAN (LOCKED — DO NOT DEVIATE)\n"
                            "The user approved the following plan. Execute "
                            "EXACTLY these parameters.\n"
                            "Any deviation is a protocol violation.\n\n"
                            f"{plan_content}\n\n"
                            "If you cannot execute as planned, STOP and report "
                            "the error. Do NOT attempt alternatives."
                        )
                    )
                ],
            }

        return None

    async def abefore_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        return self.before_model(state, runtime)

    # ── TodoList constraint builder ───────────────────────────────────

    @staticmethod
    def _build_todos_constraint(
        todos: list, state: AgentState,
    ) -> Optional[Dict[str, Any]]:
        """Serialise ``state["todos"]`` as a binding SystemMessage.

        Returns ``None`` when all todos are completed — the deep agent's
        built-in ``TodoListMiddleware`` handles natural completion.
        """
        # Classify todo statuses
        non_completed = []
        completed = []
        for todo in todos:
            status = (
                todo.get("status", "pending")
                if isinstance(todo, dict)
                else getattr(todo, "status", "pending")
            )
            title = (
                todo.get("title", "Untitled")
                if isinstance(todo, dict)
                else getattr(todo, "title", "Untitled")
            )
            if status in ("completed", "failed", "skipped"):
                completed.append(f"  ✅ {title} ({status})")
            else:
                non_completed.append(f"  ⏳ {title} ({status})")

        # ── All done → no constraint needed ─────────────────────────────
        # The deep agent's built-in TodoListMiddleware handles completion
        # naturally: the agent produces a final summary AIMessage when all
        # tasks are done.  No forced walkthrough or request_chat_continue
        # needed — the agent loop ends when the LLM stops calling tools.
        if not non_completed:
            logger.debug(
                "PlanLockMiddleware: all todos completed, no constraint needed",
                extra={"completed_count": len(completed)},
            )
            return None

        # ── Active plan → lock mode ───────────────────────────────────
        checklist_lines = non_completed + completed
        logger.debug(
            "PlanLockMiddleware: injecting active plan constraint from todos",
            extra={
                "remaining": len(non_completed),
                "completed": len(completed),
            },
        )
        return {
            "messages": [
                SystemMessage(
                    content=(
                        "## ACTIVE PLAN (LOCKED — DO NOT DEVIATE)\n"
                        "The user approved the following plan. Execute "
                        "EXACTLY these steps in order.\n"
                        "Any deviation is a protocol violation.\n\n"
                        + "### Execution Checklist\n"
                        + "\n".join(checklist_lines) + "\n\n"
                        + "### Rules\n"
                        "- Execute the next ⏳ pending/in_progress step.\n"
                        "- Update TODO status via `write_todos` as you proceed "
                        "(pending → in_progress → completed).\n"
                        "- Delegate with [PLAN-APPROVED] prefix so sub-agents "
                        "skip their own plan gate.\n"
                        "- If you cannot execute as planned, STOP and report "
                        "the error. Do NOT attempt alternatives."
                    )
                )
            ],
        }

    # ── Legacy files-based plan reader ────────────────────────────────

    @staticmethod
    def _get_active_plan_from_files(state: AgentState) -> Optional[str]:
        """Extract active plan content from state files (backward compat)."""
        files = state.get("files", {})
        if not isinstance(files, dict) or not files:
            return None

        plan_file = files.get(PlanLockMiddleware._PLAN_PATH)
        if plan_file is None:
            return None

        # Handle both string and dict file representations
        if isinstance(plan_file, str):
            return plan_file.strip() or None
        if isinstance(plan_file, dict):
            content = plan_file.get("content", "")
            return content.strip() or None

        return None




# ---------------------------------------------------------------------------
# Default limits (overridable via env vars or Config)
# ---------------------------------------------------------------------------

# Defaults (will be overridden via env vars or Config inside the factory)
_WRITE_FILE_RUN_LIMIT = 20
_GLOBAL_TOOL_RUN_LIMIT = 60
_ENABLE_TOOL_RETRY = False


# ---------------------------------------------------------------------------
# App Operator HITL — Official HumanInTheLoopMiddleware
# ---------------------------------------------------------------------------

# Production namespace patterns that trigger elevated approval warnings.
# Mirrors the legacy ArgoCDApprovalHITLMiddleware.PRODUCTION_NAMESPACES.
_PRODUCTION_NAMESPACES = {"production", "prod", "live", "prd"}


def _is_production_namespace(namespace: str) -> bool:
    """Check if a namespace matches known production patterns."""
    return str(namespace or "").strip().lower() in _PRODUCTION_NAMESPACES


def _build_approval_description(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Build a rich, human-readable description for the HITL approval card.

    Includes production namespace awareness (elevated warning when targeting
    prod-like namespaces) and detailed context for each operation type.
    """

    if tool_name == "create_application":
        app_name = tool_args.get("name") or tool_args.get("app_name", "unknown")
        project = tool_args.get("project", "default")
        namespace = (
            tool_args.get("destination_namespace")
            or tool_args.get("dest_namespace", "unknown")
        )
        repo_url = tool_args.get("repo_url", "unknown")
        path = tool_args.get("path", "unknown")
        target_revision = tool_args.get("target_revision", "HEAD")
        auto_sync = bool(tool_args.get("auto_sync", False))
        dest_server = tool_args.get(
            "destination_server", "https://kubernetes.default.svc"
        )

        is_prod = _is_production_namespace(namespace)
        header = (
            "🚨 **PRODUCTION APPLICATION CREATION — APPROVAL REQUIRED**"
            if is_prod
            else "⚠️ **APPLICATION CREATION APPROVAL REQUIRED**"
        )
        prod_warning = (
            "\n\n⚠️ You are creating an application in a **PRODUCTION** namespace."
            if is_prod
            else ""
        )

        return (
            f"{header}\n\n"
            f"**Application**: {app_name}\n"
            f"**Project**: {project}\n"
            f"**Target**: {dest_server}/{namespace}\n"
            f"**Source Repository**: {repo_url}\n"
            f"**Path**: {path}\n"
            f"**Target Revision**: {target_revision}\n"
            f"**Auto Sync**: {'enabled' if auto_sync else 'disabled'}"
            f"{prod_warning}"
        )

    elif tool_name == "update_application":
        app_name = tool_args.get("name") or tool_args.get("app_name", "unknown")
        namespace = (
            tool_args.get("destination_namespace")
            or tool_args.get("dest_namespace")
            or tool_args.get("namespace", "unknown")
        )
        is_prod = _is_production_namespace(namespace)
        header = (
            "🚨 **PRODUCTION APPLICATION UPDATE — APPROVAL REQUIRED**"
            if is_prod
            else "⚠️ **APPLICATION UPDATE APPROVAL REQUIRED**"
        )
        prod_warning = (
            "\n\n⚠️ You are updating an application in a **PRODUCTION** namespace."
            if is_prod
            else ""
        )

        return (
            f"{header}\n\n"
            f"**Application**: {app_name}\n\n"
            f"This will update the existing application configuration."
            f"{prod_warning}"
        )

    elif tool_name == "sync_application":
        app_name = tool_args.get("name") or tool_args.get("app_name", "unknown")
        namespace = (
            tool_args.get("destination_namespace")
            or tool_args.get("dest_namespace")
            or tool_args.get("namespace", "")
        )
        dry_run = bool(tool_args.get("dry_run", False))
        prune = bool(tool_args.get("prune", False))
        force = bool(tool_args.get("force", False))
        revision = tool_args.get("revision", "latest")

        is_prod = _is_production_namespace(namespace)
        header = (
            "🚨 **PRODUCTION SYNC — APPROVAL REQUIRED**"
            if is_prod
            else "🚀 **SYNC APPROVAL REQUIRED**"
        )
        prod_warning = (
            "\n\n⚠️ This will sync changes to a **PRODUCTION** namespace."
            if is_prod
            else ""
        )

        return (
            f"{header}\n\n"
            f"**Application**: {app_name}\n"
            f"**Revision**: {revision}\n"
            f"**Dry Run**: {'yes' if dry_run else 'no'}\n"
            f"**Prune**: {'yes — orphaned resources will be deleted' if prune else 'no'}\n"
            f"**Force**: {'yes' if force else 'no'}\n\n"
            f"This will deploy the application to the cluster."
            f"{prod_warning}"
        )

    elif tool_name == "delete_application":
        app_name = tool_args.get("name") or tool_args.get("app_name", "unknown")
        cascade = bool(tool_args.get("cascade", True))
        impact = (
            "This will **DELETE all Kubernetes resources** managed by this application."
            if cascade
            else "The ArgoCD Application will be removed; Kubernetes resources may be orphaned."
        )
        return (
            f"🗑️ **DELETE APPLICATION APPROVAL REQUIRED**\n\n"
            f"**Application**: {app_name}\n"
            f"**Cascade Delete**: {'yes' if cascade else 'no'}\n\n"
            f"⚠️ {impact}"
        )

    elif tool_name == "delete_project":
        project_name = tool_args.get("project_name") or tool_args.get("name", "unknown")
        return (
            f"🗑️ **DELETE PROJECT APPROVAL REQUIRED**\n\n"
            f"**Project**: {project_name}\n\n"
            f"⚠️ Destructive action. Applications in this project may need "
            f"to be removed first. Cannot be undone."
        )

    elif tool_name == "delete_repository":
        repo_url = tool_args.get("repo_url", "unknown")
        return (
            f"🗑️ **DELETE REPOSITORY APPROVAL REQUIRED**\n\n"
            f"**Repository**: {repo_url}\n\n"
            f"⚠️ This removes the repository registration from ArgoCD. "
            f"Applications using this repo may lose their source."
        )

    elif tool_name == "onboard_repository_https":
        repo_url = tool_args.get("repo_url", "unknown")
        project = tool_args.get("project", "")
        return (
            f"📦 **REPOSITORY ONBOARDING (CREATE / UPDATE) — APPROVAL REQUIRED**\n\n"
            f"**Repository**: {repo_url}\n"
            f"**Project**: {project or 'default'}\n\n"
            f"This will register or update the Git repository with ArgoCD via HTTPS. "
            f"It will be available as a source for applications."
        )

    elif tool_name == "onboard_repository_ssh":
        repo_url = tool_args.get("repo_url", "unknown")
        project = tool_args.get("project", "")
        return (
            f"📦 **REPOSITORY ONBOARDING (CREATE / UPDATE) — APPROVAL REQUIRED**\n\n"
            f"**Repository**: {repo_url}\n"
            f"**Project**: {project or 'default'}\n\n"
            f"This will register or update the Git repository with ArgoCD via SSH. "
            f"Requires SSH_PRIVATE_KEY_PATH environment variable."
        )

    elif tool_name == "create_project":
        project_name = (
            tool_args.get("project_name")
            or tool_args.get("name", "unknown")
        )
        description = tool_args.get("description", "")
        return (
            f"📁 **CREATE / UPDATE PROJECT — APPROVAL REQUIRED**\n\n"
            f"**Project**: {project_name}\n"
            f"**Description**: {description or 'n/a'}\n\n"
            f"This will create or update an ArgoCD AppProject with RBAC policies and source repository restrictions."
        )

    else:
        return f"Approval required for {tool_name}."


# ---------------------------------------------------------------------------
# Argo Rollouts — approval card descriptions
# ---------------------------------------------------------------------------

def _build_rollouts_approval_description(
    tool_name: str, tool_args: Dict[str, Any]
) -> str:
    """Build a rich, human-readable description for Argo Rollouts HITL cards.

    Production-namespace-aware, apply-flag-gated, and action-differentiated.
    Covers: migrations, lifecycle actions, destructive deletes, analysis
    template creation, fresh rollout creation, and legacy deployment mutations.
    """

    ns = tool_args.get("namespace", "unknown")
    name = tool_args.get("name", tool_args.get("rollout_name", "unknown"))
    is_prod = _is_production_namespace(ns)

    def _prod_header(action_emoji: str, action_title: str) -> str:
        if is_prod:
            return f"🚨 **PRODUCTION {action_title} — APPROVAL REQUIRED**"
        return f"{action_emoji} **{action_title} — APPROVAL REQUIRED**"

    def _prod_warning() -> str:
        if is_prod:
            return f"\n\n⚠️ Target namespace `{ns}` is a **PRODUCTION** environment."
        return ""

    # -- Destructive: Delete Rollout ----------------------------------------
    if tool_name == "argo_delete_rollout":
        return (
            f"{_prod_header('🗑️', 'DELETE ROLLOUT')}\n\n"
            f"**Rollout**: {name}\n"
            f"**Namespace**: {ns}\n\n"
            f"⚠️ This removes the Rollout CRD, all managed ReplicaSets, "
            f"and associated canary/stable Services."
            f"{_prod_warning()}"
        )

    # -- Destructive: Delete Experiment -------------------------------------
    elif tool_name == "argo_delete_experiment":
        return (
            f"{_prod_header('🗑️', 'DELETE EXPERIMENT')}\n\n"
            f"**Experiment**: {name}\n"
            f"**Namespace**: {ns}\n\n"
            f"⚠️ This tears down all experiment pods (baseline + candidate) "
            f"and associated AnalysisRuns."
            f"{_prod_warning()}"
        )

    # -- Migration: Deployment → Rollout ------------------------------------
    elif tool_name == "convert_deployment_to_rollout":
        mode = tool_args.get("mode", "direct")
        deployment = tool_args.get("deployment_name", name)
        apply_flag = tool_args.get("apply", False)
        strategy = tool_args.get("strategy", "canary")

        if not apply_flag:
            # apply=False is a read-only YAML preview — low-risk description
            return (
                f"📄 **MIGRATION PREVIEW** (read-only)\n\n"
                f"**Deployment → Rollout**: {deployment}\n"
                f"**Namespace**: {ns}\n"
                f"**Mode**: {mode}\n"
                f"**Strategy**: {strategy}\n\n"
                f"This generates YAML for review. No cluster changes."
            )

        mode_detail = (
            "Rollout references existing Deployment via workloadRef — no pod duplication."
            if mode == "workloadRef"
            else "Deployment will be replaced by Rollout CRD — pods transition to Rollout ownership."
        )
        return (
            f"{_prod_header('🔄', 'MIGRATION')}\n\n"
            f"**Deployment → Rollout**: {deployment}\n"
            f"**Namespace**: {ns}\n"
            f"**Mode**: {mode}\n"
            f"**Strategy**: {strategy}\n"
            f"**Apply**: yes — will modify the cluster\n\n"
            f"⚠️ {mode_detail}"
            f"{_prod_warning()}"
        )

    # -- Migration: Rollout → Deployment (reverse) --------------------------
    elif tool_name == "convert_rollout_to_deployment":
        apply_flag = tool_args.get("apply", False)

        if not apply_flag:
            return (
                f"📄 **REVERSE MIGRATION PREVIEW** (read-only)\n\n"
                f"**Rollout → Deployment**: {name}\n"
                f"**Namespace**: {ns}\n\n"
                f"This generates YAML for review. No cluster changes."
            )

        return (
            f"{_prod_header('⏪', 'REVERSE MIGRATION')}\n\n"
            f"**Rollout → Deployment**: {name}\n"
            f"**Namespace**: {ns}\n\n"
            f"⚠️ This reverts to a standard Kubernetes Deployment. "
            f"Canary/blue-green capabilities will be permanently removed."
            f"{_prod_warning()}"
        )

    # -- Lifecycle: promote / promote_full / abort / skip_analysis ----------
    elif tool_name == "argo_manage_rollout_lifecycle":
        action = tool_args.get("action", "unknown")

        if action == "promote_full":
            return (
                f"{_prod_header('🚨', 'FULL PROMOTION')}\n\n"
                f"**Rollout**: {name}\n"
                f"**Namespace**: {ns}\n"
                f"**Action**: `promote_full`\n\n"
                f"⚠️ This commits **100% traffic** to the canary version, "
                f"skipping all remaining analysis gates and pause steps. "
                f"This action is **irreversible** without a new deployment."
                f"{_prod_warning()}"
            )
        elif action == "abort":
            return (
                f"🛑 **ROLLOUT ABORT — APPROVAL REQUIRED**\n\n"
                f"**Rollout**: {name}\n"
                f"**Namespace**: {ns}\n"
                f"**Action**: `abort`\n\n"
                f"⚡ Immediately returns **all traffic to the stable** ReplicaSet. "
                f"Canary pods will be scaled down. The new image is preserved "
                f"for retry after root cause analysis."
                f"{_prod_warning()}"
            )
        elif action == "skip_analysis":
            return (
                f"🚨 **SKIP ANALYSIS — HIGHEST RISK — APPROVAL REQUIRED**\n\n"
                f"**Rollout**: {name}\n"
                f"**Namespace**: {ns}\n"
                f"**Action**: `skip_analysis`\n\n"
                f"⚠️ This **bypasses Prometheus safety gates**. The canary "
                f"version will proceed without metric validation. Only invoke "
                f"when Prometheus is down or the version is pre-verified."
                f"{_prod_warning()}"
            )
        elif action == "promote":
            return (
                f"🚀 **CANARY PROMOTION — APPROVAL REQUIRED**\n\n"
                f"**Rollout**: {name}\n"
                f"**Namespace**: {ns}\n"
                f"**Action**: `promote` (advance to next canary step)\n\n"
                f"This advances the rollout to the next step in the canary "
                f"weight progression."
                f"{_prod_warning()}"
            )
        else:
            # pause, resume, or other actions
            return (
                f"⚡ **ROLLOUT LIFECYCLE — APPROVAL REQUIRED**\n\n"
                f"**Rollout**: {name}\n"
                f"**Namespace**: {ns}\n"
                f"**Action**: `{action}`"
                f"{_prod_warning()}"
            )

    # -- Legacy Deployment mutation -----------------------------------------
    elif tool_name == "argo_manage_legacy_deployment":
        action = tool_args.get("action", "unknown")
        deployment = tool_args.get("deployment_name", name)

        if action == "generate_scale_down_manifest":
            return (
                f"📄 **SCALE-DOWN MANIFEST GENERATION**\n\n"
                f"**Deployment**: {deployment}\n"
                f"**Namespace**: {ns}\n\n"
                f"Generates a patch to scale replicas to 0. Safe — "
                f"commit to Git and let ArgoCD apply it."
            )

        return (
            f"{_prod_header('⚠️', 'LEGACY DEPLOYMENT MUTATION')}\n\n"
            f"**Deployment**: {deployment}\n"
            f"**Namespace**: {ns}\n"
            f"**Action**: `{action}`\n\n"
            f"⚠️ Direct cluster mutation. For ArgoCD-managed apps, prefer "
            f"`generate_scale_down_manifest` → commit to Git instead."
            f"{_prod_warning()}"
        )

    # -- Create Rollout (fresh, no migration) -------------------------------
    elif tool_name == "argo_create_rollout":
        strategy = tool_args.get("strategy", "canary")
        image = tool_args.get("image", "unknown")
        return (
            f"{_prod_header('🆕', 'CREATE ROLLOUT')}\n\n"
            f"**Rollout**: {name}\n"
            f"**Namespace**: {ns}\n"
            f"**Strategy**: {strategy}\n"
            f"**Image**: {image}\n\n"
            f"⚠️ This creates a new Rollout CRD on the cluster. "
            f"A new ReplicaSet and associated Services will be created."
            f"{_prod_warning()}"
        )

    # -- Configure AnalysisTemplate ----------------------------------------
    elif tool_name == "argo_configure_analysis_template":
        mode = tool_args.get("mode", "generate_yaml")
        if mode == "generate_yaml":
            return (
                f"📄 **ANALYSIS TEMPLATE PREVIEW** (read-only)\n\n"
                f"**Rollout**: {name}\n"
                f"**Namespace**: {ns}\n\n"
                f"Generates AnalysisTemplate YAML for review. No cluster changes."
            )
        return (
            f"{_prod_header('📊', 'CONFIGURE ANALYSIS TEMPLATE')}\n\n"
            f"**Rollout**: {name}\n"
            f"**Namespace**: {ns}\n"
            f"**Mode**: `execute` — will create AnalysisTemplate on cluster\n\n"
            f"⚠️ This creates a Prometheus-backed AnalysisTemplate and links "
            f"it to the Rollout. Future promotions will be gated by metric "
            f"thresholds defined in this template."
            f"{_prod_warning()}"
        )

    # -- Create stable/canary Services -------------------------------------
    elif tool_name == "create_stable_canary_services":
        apply_flag = tool_args.get("apply", False)
        if not apply_flag:
            return (
                f"📄 **SERVICE GENERATION PREVIEW** (read-only)\n\n"
                f"**Namespace**: {ns}\n\n"
                f"Generates stable + canary Service YAML for review."
            )
        return (
            f"{_prod_header('🔗', 'CREATE STABLE/CANARY SERVICES')}\n\n"
            f"**Namespace**: {ns}\n"
            f"**Apply**: yes — will create Services on the cluster\n\n"
            f"⚠️ This creates stable and canary Service objects for traffic routing."
            f"{_prod_warning()}"
        )

    else:
        return f"Approval required for Argo Rollouts operation: {tool_name}."


# ---------------------------------------------------------------------------
# DRY helper — builds InterruptOnConfig with a standard description lambda
# ---------------------------------------------------------------------------

def _make_interrupt_config(
    tool_name: str,
    description_builder: Any,
    *,
    allowed_decisions: Any = None,
) -> InterruptOnConfig:
    """Create an ``InterruptOnConfig`` for a tool with a description builder.

    This avoids repeating the lambda pattern for every tool entry.
    """
    return InterruptOnConfig(
        allowed_decisions=allowed_decisions or ["approve", "reject"],
        description=lambda tool_call, state, runtime, _tn=tool_name, _db=description_builder: (
            _db(_tn, tool_call.get("args", {}))
        ),
    )


# ---------------------------------------------------------------------------
# Middleware factories — one per domain
# ---------------------------------------------------------------------------

def build_app_operator_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Create a ``HumanInTheLoopMiddleware`` configured for ArgoCD operations."""
    logger.info("Building HumanInTheLoopMiddleware for ArgoCD execution tools")

    return HumanInTheLoopMiddleware(
        interrupt_on={
            "create_application": _make_interrupt_config(
                "create_application", _build_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "update_application": _make_interrupt_config(
                "update_application", _build_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "sync_application": _make_interrupt_config(
                "sync_application", _build_approval_description,
            ),
            "delete_application": _make_interrupt_config(
                "delete_application", _build_approval_description,
            ),
            "delete_project": _make_interrupt_config(
                "delete_project", _build_approval_description,
            ),
            "delete_repository": _make_interrupt_config(
                "delete_repository", _build_approval_description,
            ),
            "onboard_repository_https": _make_interrupt_config(
                "onboard_repository_https", _build_approval_description,
            ),
            "onboard_repository_ssh": _make_interrupt_config(
                "onboard_repository_ssh", _build_approval_description,
            ),
            "create_project": _make_interrupt_config(
                "create_project", _build_approval_description,
            ),
        },
        description_prefix="⚠️ ArgoCD Operation — Approval Required",
    )


def build_argo_rollouts_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Create a ``HumanInTheLoopMiddleware`` configured for Argo Rollouts operations.

    Gated tools (from SKILL.md safety rules and progressive delivery best practices):
        - argo_delete_rollout — destructive: removes CRD + all ReplicaSets
        - argo_delete_experiment — destructive: tears down experiment pods
        - convert_deployment_to_rollout — migration (apply=True mutates cluster)
        - convert_rollout_to_deployment — reverse migration (apply=True)
        - argo_manage_rollout_lifecycle — promote_full/abort/skip_analysis are high-risk
        - argo_manage_legacy_deployment — direct cluster mutation
        - argo_create_rollout — creates new Rollout CRD (cluster mutation)
        - argo_configure_analysis_template — mode=execute creates AnalysisTemplate
        - create_stable_canary_services — apply=True creates Services
        - argo_update_rollout — image/spec update on live rollout (FINDING 5)
    """
    logger.info("Building HumanInTheLoopMiddleware for Argo Rollouts execution tools")

    return HumanInTheLoopMiddleware(
        interrupt_on={
            "argo_delete_rollout": _make_interrupt_config(
                "argo_delete_rollout", _build_rollouts_approval_description,
            ),
            "argo_delete_experiment": _make_interrupt_config(
                "argo_delete_experiment", _build_rollouts_approval_description,
            ),
            "convert_deployment_to_rollout": _make_interrupt_config(
                "convert_deployment_to_rollout", _build_rollouts_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "convert_rollout_to_deployment": _make_interrupt_config(
                "convert_rollout_to_deployment", _build_rollouts_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
            "argo_manage_rollout_lifecycle": _make_interrupt_config(
                "argo_manage_rollout_lifecycle", _build_rollouts_approval_description,
            ),
            "argo_manage_legacy_deployment": _make_interrupt_config(
                "argo_manage_legacy_deployment", _build_rollouts_approval_description,
            ),
            "argo_create_rollout": _make_interrupt_config(
                "argo_create_rollout", _build_rollouts_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "argo_configure_analysis_template": _make_interrupt_config(
                "argo_configure_analysis_template", _build_rollouts_approval_description,
            ),
            "create_stable_canary_services": _make_interrupt_config(
                "create_stable_canary_services", _build_rollouts_approval_description,
            ),
            "argo_update_rollout": _make_interrupt_config(
                "argo_update_rollout", _build_rollouts_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
        },
        description_prefix="⚠️ Argo Rollouts Operation — Approval Required",
    )


# ---------------------------------------------------------------------------
# Traefik — approval card descriptions
# ---------------------------------------------------------------------------

def _build_traefik_approval_description(
    tool_name: str, tool_args: Dict[str, Any]
) -> str:
    """Build a rich, human-readable description for Traefik HITL cards.

    Production-namespace-aware and action-differentiated. Covers: weighted
    routing, simple routes, middleware, NGINX migration, TCP routing, and
    service affinity changes.
    """

    ns = tool_args.get("namespace", "unknown")
    action = tool_args.get("action", "unknown")
    route_name = tool_args.get("route_name", tool_args.get("name", "unknown"))
    is_prod = _is_production_namespace(ns)

    def _prod_header(action_emoji: str, action_title: str) -> str:
        if is_prod:
            return f"🚨 **PRODUCTION {action_title} — APPROVAL REQUIRED**"
        return f"{action_emoji} **{action_title} — APPROVAL REQUIRED**"

    def _prod_warning() -> str:
        if is_prod:
            return f"\n\n⚠️ Target namespace `{ns}` is a **PRODUCTION** environment."
        return ""

    # -- Weighted routing (canary traffic) ----------------------------------
    if tool_name == "traefik_manage_weighted_routing":
        stable = tool_args.get("stable_service", "")
        canary = tool_args.get("canary_service", "")
        s_weight = tool_args.get("stable_weight", "?")
        c_weight = tool_args.get("canary_weight", "?")
        hostname = tool_args.get("hostname", "")

        if action == "delete":
            return (
                f"{_prod_header('🗑️', 'DELETE WEIGHTED ROUTE')}\n\n"
                f"**Route**: {route_name}\n"
                f"**Namespace**: {ns}\n\n"
                f"⚠️ This removes the TraefikService + IngressRoute pair. "
                f"All traffic through this route will stop immediately."
                f"{_prod_warning()}"
            )
        return (
            f"{_prod_header('🔀', f'WEIGHTED ROUTING {action.upper()}')}\n\n"
            f"**Route**: {route_name}\n"
            f"**Namespace**: {ns}\n"
            f"**Host**: {hostname or 'n/a'}\n"
            f"**Stable**: {stable} → {s_weight}%\n"
            f"**Canary**: {canary} → {c_weight}%\n\n"
            f"⚠️ This affects live traffic distribution."
            f"{_prod_warning()}"
        )

    # -- Simple route (direct IngressRoute) ---------------------------------
    elif tool_name == "traefik_manage_simple_route":
        svc = tool_args.get("service_name", "unknown")
        if action == "delete":
            return (
                f"{_prod_header('🗑️', 'DELETE SIMPLE ROUTE')}\n\n"
                f"**Route**: {route_name}\n"
                f"**Namespace**: {ns}\n\n"
                f"⚠️ This removes the IngressRoute. Traffic to this endpoint will stop."
                f"{_prod_warning()}"
            )
        return (
            f"{_prod_header('🔗', 'SIMPLE ROUTE CREATE')}\n\n"
            f"**Route**: {route_name}\n"
            f"**Namespace**: {ns}\n"
            f"**Service**: {svc}\n\n"
            f"⚠️ Upsert — will overwrite if route already exists."
            f"{_prod_warning()}"
        )

    # -- Middleware CRD management ------------------------------------------
    elif tool_name == "traefik_manage_middleware":
        mw_name = tool_args.get("middleware_name", "unknown")
        mw_type = tool_args.get("middleware_type", "unknown")
        if action == "delete":
            return (
                f"{_prod_header('🗑️', 'DELETE MIDDLEWARE')}\n\n"
                f"**Middleware**: {mw_name}\n"
                f"**Namespace**: {ns}\n"
                f"**Type**: {mw_type}\n\n"
                f"⚠️ Routes referencing this middleware will lose its protection "
                f"(rate limiting, auth, etc.)."
                f"{_prod_warning()}"
            )
        return (
            f"{_prod_header('🛡️', f'MIDDLEWARE {action.upper()}')}\n\n"
            f"**Middleware**: {mw_name}\n"
            f"**Namespace**: {ns}\n"
            f"**Type**: {mw_type}\n\n"
            f"⚠️ This {'creates' if action == 'create' else 'updates'} "
            f"a middleware CRD on the cluster."
            f"{_prod_warning()}"
        )

    # -- NGINX migration ----------------------------------------------------
    elif tool_name == "traefik_nginx_migration":
        ingress_name = tool_args.get("ingress_name", "")
        migration_plan = tool_args.get("migration_plan", None)

        if action == "generate":
            return (
                f"📄 **NGINX MIGRATION PREVIEW** (read-only)\n\n"
                f"**Namespace**: {ns}\n\n"
                f"Generates Traefik CRD YAML for review. No cluster changes."
            )
        elif action == "apply":
            plan_note = " with custom overrides" if migration_plan else ""
            return (
                f"{_prod_header('🔄', 'NGINX MIGRATION APPLY')}\n\n"
                f"**Namespace**: {ns}{plan_note}\n\n"
                f"⚠️ This creates Traefik CRDs and patches NGINX Ingresses. "
                f"Requires MCP_ALLOW_WRITE=true. Cannot be undone without revert."
                f"{_prod_warning()}"
            )
        elif action == "revert":
            return (
                f"{_prod_header('⏪', 'NGINX MIGRATION REVERT')}\n\n"
                f"**Ingress**: {ingress_name or 'all in namespace'}\n"
                f"**Namespace**: {ns}\n\n"
                f"⚠️ This restores original NGINX Ingress config and removes "
                f"corresponding Traefik CRDs."
                f"{_prod_warning()}"
            )
        return (
            f"📄 **NGINX MIGRATION**\n\n"
            f"**Namespace**: {ns}\n"
            f"**Action**: `{action}`"
        )

    # -- TCP routing --------------------------------------------------------
    elif tool_name == "traefik_manage_tcp_routing":
        svc = tool_args.get("service_name", "unknown")
        port = tool_args.get("service_port", "?")
        if action == "delete":
            return (
                f"{_prod_header('🗑️', 'DELETE TCP ROUTE')}\n\n"
                f"**Route**: {route_name}\n"
                f"**Namespace**: {ns}\n\n"
                f"⚠️ TCP routes have **NO health-based rollback**. "
                f"Deleting will immediately cut TCP connectivity."
                f"{_prod_warning()}"
            )
        return (
            f"{_prod_header('🔌', 'TCP ROUTE CREATE')}\n\n"
            f"**Route**: {route_name}\n"
            f"**Namespace**: {ns}\n"
            f"**Backend**: {svc}:{port}\n\n"
            f"⚠️ TCP routes have no rollback. Verify service availability first."
            f"{_prod_warning()}"
        )

    # -- Sticky sessions (service affinity) ---------------------------------
    elif tool_name == "traefik_configure_service_affinity":
        svc = tool_args.get("service_name", "unknown")
        if action == "disable":
            return (
                f"{_prod_header('⚠️', 'DISABLE STICKY SESSIONS')}\n\n"
                f"**Service**: {svc}\n"
                f"**Namespace**: {ns}\n\n"
                f"⚠️ Removing session affinity will redistribute all users "
                f"across pods. Stateful sessions may be lost."
                f"{_prod_warning()}"
            )
        cookie = tool_args.get("cookie_name", "_traefik_backend")
        return (
            f"{_prod_header('📌', 'ENABLE STICKY SESSIONS')}\n\n"
            f"**Service**: {svc}\n"
            f"**Namespace**: {ns}\n"
            f"**Cookie**: {cookie}\n\n"
            f"⚠️ Users will be pinned to specific backend pods."
            f"{_prod_warning()}"
        )

    else:
        return f"Approval required for Traefik operation: {tool_name}."


def build_traefik_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Create a ``HumanInTheLoopMiddleware`` configured for Traefik operations.

    Gated tools (from SKILL.md safety rule #6):
        - traefik_manage_weighted_routing — live traffic routing
        - traefik_manage_simple_route — route create (upsert) / delete
        - traefik_manage_middleware — create/update/delete middleware CRDs
        - traefik_nginx_migration — apply and revert are cluster mutations
        - traefik_manage_tcp_routing — TCP has no health rollback (rule #9)
        - traefik_configure_service_affinity — disable loses sticky state
        - traefik_generate_routing_manifest — generate+apply routing (FINDING 7)
    """
    logger.info("Building HumanInTheLoopMiddleware for Traefik execution tools")

    return HumanInTheLoopMiddleware(
        interrupt_on={
            "traefik_manage_weighted_routing": _make_interrupt_config(
                "traefik_manage_weighted_routing", _build_traefik_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "traefik_manage_simple_route": _make_interrupt_config(
                "traefik_manage_simple_route", _build_traefik_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "traefik_manage_middleware": _make_interrupt_config(
                "traefik_manage_middleware", _build_traefik_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "traefik_nginx_migration": _make_interrupt_config(
                "traefik_nginx_migration", _build_traefik_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
            "traefik_manage_tcp_routing": _make_interrupt_config(
                "traefik_manage_tcp_routing", _build_traefik_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
            "traefik_configure_service_affinity": _make_interrupt_config(
                "traefik_configure_service_affinity", _build_traefik_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
            "traefik_generate_routing_manifest": _make_interrupt_config(
                "traefik_generate_routing_manifest", _build_traefik_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
        },
        description_prefix="⚠️ Traefik Operation — Approval Required",
    )


def build_app_operator_middleware(
    config: Optional["Config"] = None,
    *,
    write_file_limit: Optional[int] = None,
    global_tool_limit: Optional[int] = None,
    enable_tool_retry: Optional[bool] = None,
    extra_middleware: Optional[List[Any]] = None,
    model: Optional[str] = None,
    backend: Optional[Any] = None,
) -> List[Any]:
    """Assemble the middleware stack for the AppOperatorCoordinator deep agent."""
    from langchain.agents.middleware import (
        ToolCallLimitMiddleware,
        ToolRetryMiddleware,
    )

    middleware: List[Any] = []

    # 0. Operation context injection
    middleware.append(AppOperationContextMiddleware())
    logger.info("Middleware: AppOperationContextMiddleware (before_model)")

    # 0b. Plan lock enforcement (re-injects approved plan before every model call)
    middleware.append(PlanLockMiddleware())
    logger.info("Middleware: PlanLockMiddleware (before_model)")

    # 1. Per-tool write_file guard
    env_wf_limit = os.getenv("APP_OP_WRITE_FILE_RUN_LIMIT")
    wf_limit = write_file_limit or (int(env_wf_limit) if env_wf_limit else _WRITE_FILE_RUN_LIMIT)
    middleware.append(
        ToolCallLimitMiddleware(
            tool_name="write_file",
            run_limit=wf_limit,
            exit_behavior="end",
        )
    )

    # 2. Global tool call guard
    env_gt_limit = os.getenv("APP_OP_GLOBAL_TOOL_RUN_LIMIT")
    gt_limit = global_tool_limit or (int(env_gt_limit) if env_gt_limit else _GLOBAL_TOOL_RUN_LIMIT)
    middleware.append(
        ToolCallLimitMiddleware(
            run_limit=gt_limit,
            exit_behavior="end",
        )
    )

    # 3. Model call guard — REMOVED
    # ModelCallLimitMiddleware was silently terminating the deep agent
    # (exit_behavior="end") before it could produce a final summary,
    # causing the agent to appear "stuck" after completing operations.
    # LangGraph's recursion_limit and the global ToolCallLimitMiddleware
    # above provide sufficient safety nets against runaway loops.

    # 4. Tool retry (transient failures)
    env_retry = os.getenv("APP_OP_ENABLE_TOOL_RETRY")
    should_retry = enable_tool_retry if enable_tool_retry is not None else (
        env_retry.lower() == "true" if env_retry else _ENABLE_TOOL_RETRY
    )
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

    # 5. Shared coordinator middleware (CodeInterpreter + Summarization)
    from k8s_autopilot.core.agents.shared_middleware import (
        build_shared_coordinator_middleware,
    )
    middleware.extend(
        build_shared_coordinator_middleware(
            model=model,
            backend=backend,
            config=config,
        )
    )

    # 6. Extra middleware
    if extra_middleware:
        middleware.extend(extra_middleware)

    return middleware
