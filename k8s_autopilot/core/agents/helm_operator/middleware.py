"""
Helm Operator — Modular middleware factory for deep agent safety nets.

Provides a configurable factory that assembles middleware stacks for the
HelmOperatorCoordinator deep agent.  Each middleware is independently
toggleable via ``Config`` or environment variables, keeping the coordinator clean.

Middleware catalogue:
    ToolCallLimitMiddleware   — caps runaway tool loops (per-tool or global)
    ModelCallLimitMiddleware  — caps excessive LLM calls
    ToolRetryMiddleware       — auto-retries transient tool failures
    SummarizationMiddleware   — compresses context when window fills

Usage::

    from k8s_autopilot.core.agents.helm_operator.middleware import (
        build_k8s_middleware,
    )

    middleware = build_k8s_middleware(config)
    agent = create_deep_agent(
        ...,
        middleware=middleware,
    )

Reference: aws-orchestrator-agent tf_operator/middleware.py
"""
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("K8sMiddleware")


# ---------------------------------------------------------------------------
# Layer 2: OperationContextMiddleware — survives summarization
# ---------------------------------------------------------------------------
#
# This middleware reads the operations journal from the agent's virtual
# filesystem state and re-injects it as a SystemMessage BEFORE EVERY
# coordinator model call.  This means:
#
#   1. Even after the built-in summarization compresses conversation
#      history, the coordinator ALWAYS sees recent operation context.
#   2. The coordinator can always include full details (chart source,
#      release name, namespace, values) when delegating follow-up tasks
#      to the helm-operation subagent.
#
# This is the key mechanism that prevents the "lost chart URL" problem.
#
# Reference: LangChain docs — Custom Middleware → before_model hook
#            https://docs.langchain.com/oss/python/langchain/middleware/custom
# ---------------------------------------------------------------------------

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import SystemMessage


class OperationContextMiddleware(AgentMiddleware):
    """Injects recent helm operation context before every model call.

    Reads ``/memories/helm-operator/operations-log.md`` from the agent's
    ``state["files"]`` and prepends a compact SystemMessage with recent
    operation details.  This context survives the deep agent's built-in
    summarization because it is re-injected on every model call, not
    stored only in conversation history.

    Usage::

        middleware = [OperationContextMiddleware(), ...]
        agent = create_deep_agent(middleware=middleware, ...)
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Read operations journal and inject as SystemMessage."""
        from k8s_autopilot.utils.operations_context import (
            get_operations_context_from_state,
        )

        ops_context = get_operations_context_from_state(dict(state))
        if not ops_context:
            return None

        logger.debug(
            "OperationContextMiddleware: injecting operations context",
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
                        "requests. NEVER re-ask the user for chart source, "
                        "release name, namespace, or values that are "
                        "listed here.\n\n"
                        f"{ops_context}"
                    )
                )
            ],
        }

    async def abefore_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Async version — delegates to sync implementation."""
        return self.before_model(state, runtime)


# ---------------------------------------------------------------------------
# Default limits (overridable via env vars or Config)
# ---------------------------------------------------------------------------

# Per-tool write_file limit — prevents infinite write-retry loops
_WRITE_FILE_RUN_LIMIT = int(os.getenv("K8S_WRITE_FILE_RUN_LIMIT", "20"))

# Global tool call limit per single invocation
_GLOBAL_TOOL_RUN_LIMIT = int(os.getenv("K8S_GLOBAL_TOOL_RUN_LIMIT", "60"))

# Model call limit per invocation
_MODEL_CALL_RUN_LIMIT = int(os.getenv("K8S_MODEL_CALL_RUN_LIMIT", "40"))

# Whether to enable tool retry middleware (disabled by default as it swallows GraphInterrupt)
_ENABLE_TOOL_RETRY = os.getenv("K8S_ENABLE_TOOL_RETRY", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Helm HITL — Official HumanInTheLoopMiddleware
# ---------------------------------------------------------------------------
#
# Uses LangChain's built-in HumanInTheLoopMiddleware instead of a custom
# interrupt()-based implementation. This provides:
#   - Structured decision handling (approve / edit / reject)
#   - Proper Command(resume={"decisions": [...]}) integration
#   - Action batching when multiple tools trigger simultaneously
#   - Standard deep-agent harness compatibility
#
# The ``description`` callables generate rich, per-operation approval cards
# so the human reviewer sees chart name, release, namespace, etc.
#
# Reference: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
# ---------------------------------------------------------------------------

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig



# Tools that require human approval before execution on live clusters.
HELM_APPROVAL_REQUIRED_TOOLS = frozenset({
    "helm_install_chart",
    "helm_upgrade_release",
    "helm_rollback_release",
    "helm_uninstall_release",
})


def _build_approval_description(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Build a rich, human-readable description for the HITL approval card.

    This is passed as the ``description`` callable to
    ``HumanInTheLoopMiddleware`` so each tool type gets a contextual
    approval message with chart, release, namespace, and revision details.
    """
    chart_name = tool_args.get("chart_name", "unknown")
    release_name = tool_args.get("release_name", "unknown")
    namespace = tool_args.get("namespace", "default")

    if tool_name == "helm_install_chart":
        repo = tool_args.get("repository")
        if not repo and "/" in chart_name:
            repo = chart_name.split("/")[0]
        elif not repo:
            repo = chart_name

        chart_display = chart_name if "/" in chart_name else f"{repo}/{chart_name}"

        return (
            f"⚠️ **INSTALLATION APPROVAL REQUIRED**\n\n"
            f"**Chart**: {chart_display}\n"
            f"**Release**: {release_name}\n"
            f"**Namespace**: {namespace}\n"
            f"**Repository**: {repo}"
        )
    elif tool_name == "helm_upgrade_release":
        return (
            f"⚠️ **UPGRADE APPROVAL REQUIRED**\n\n"
            f"**Release**: {release_name}\n"
            f"**Chart**: {chart_name}\n"
            f"**Namespace**: {namespace}"
        )
    elif tool_name == "helm_rollback_release":
        revision = tool_args.get("revision", "previous")
        return (
            f"⏪ **ROLLBACK APPROVAL REQUIRED**\n\n"
            f"**Release**: {release_name}\n"
            f"**Namespace**: {namespace}\n"
            f"**Target Revision**: {revision}"
        )
    elif tool_name == "helm_uninstall_release":
        return (
            f"🗑️ **UNINSTALL APPROVAL REQUIRED**\n\n"
            f"**Release**: {release_name}\n"
            f"**Namespace**: {namespace}\n\n"
            f"⚠️ This will remove the release and all its resources."
        )
    else:
        return f"Approval required for {tool_name}."


def build_helm_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Create a ``HumanInTheLoopMiddleware`` configured for Helm operations.

    Gates every destructive Helm tool behind a structured HITL interrupt:
    - ``helm_install_chart``: approve / edit / reject (edit lets user tweak values)
    - ``helm_upgrade_release``: approve / edit / reject
    - ``helm_rollback_release``: approve / reject only (no arg editing for rollbacks)
    - ``helm_uninstall_release``: approve / reject only

    The middleware auto-batches interrupts when the LLM proposes multiple
    gated tool calls in the same model response.

    Returns:
        Configured ``HumanInTheLoopMiddleware`` instance ready for
        ``create_agent(middleware=[...])`` or ``_build_mcp_subagent()``.
    """
    logger.info("Building HumanInTheLoopMiddleware for Helm execution tools")

    return HumanInTheLoopMiddleware(
        interrupt_on={
            "helm_install_chart": InterruptOnConfig(
                allowed_decisions=["approve", "edit", "reject"],
                description=lambda tool_call, state, runtime: _build_approval_description(
                    "helm_install_chart", tool_call.get("args", {}),
                ),
            ),
            "helm_upgrade_release": InterruptOnConfig(
                allowed_decisions=["approve", "edit", "reject"],
                description=lambda tool_call, state, runtime: _build_approval_description(
                    "helm_upgrade_release", tool_call.get("args", {}),
                ),
            ),
            "helm_rollback_release": InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                description=lambda tool_call, state, runtime: _build_approval_description(
                    "helm_rollback_release", tool_call.get("args", {}),
                ),
            ),
            "helm_uninstall_release": InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                description=lambda tool_call, state, runtime: _build_approval_description(
                    "helm_uninstall_release", tool_call.get("args", {}),
                ),
            ),
        },
        description_prefix="⚠️ Helm Cluster Operation — Approval Required",
    )

def build_k8s_middleware(
    config: Optional["Config"] = None,
    *,
    write_file_limit: Optional[int] = None,
    global_tool_limit: Optional[int] = None,
    model_call_limit: Optional[int] = None,
    enable_tool_retry: Optional[bool] = None,
    extra_middleware: Optional[List[Any]] = None,
) -> List[Any]:
    """Assemble the middleware stack for the HelmOperatorCoordinator deep agent.

    Each guard is independently configurable.  Pass ``None`` to use the
    default (env var → compiled default).

    Args:
        config: Application config (reserved for future per-agent overrides).
        write_file_limit: Max ``write_file`` calls per run.
        global_tool_limit: Max total tool calls per run (all tools).
        model_call_limit: Max LLM calls per run.
        enable_tool_retry: Whether to auto-retry transient tool failures.
        extra_middleware: Additional middleware instances to append.

    Returns:
        A list of middleware instances suitable for
        ``create_deep_agent(middleware=...)``.
    """
    from langchain.agents.middleware import (
        ToolCallLimitMiddleware,
        ModelCallLimitMiddleware,
        ToolRetryMiddleware,
    )

    middleware: List[Any] = []

    # ── 0. Operation context injection (survives summarization) ────────────
    # MUST be first so the coordinator always sees recent operation context
    # even after the built-in summarization compresses conversation history.
    middleware.append(OperationContextMiddleware())
    logger.info("Middleware: OperationContextMiddleware (before_model)")

    # ── 1. Per-tool write_file guard ──────────────────────────────────────
    wf_limit = write_file_limit or _WRITE_FILE_RUN_LIMIT
    middleware.append(
        ToolCallLimitMiddleware(
            tool_name="write_file",
            run_limit=wf_limit,
            exit_behavior="end",  # Graceful stop; LLM sees the limit message
        )
    )
    logger.info(
        "Middleware: write_file limit",
        extra={"run_limit": wf_limit},
    )

    # ── 2. Global tool call guard ─────────────────────────────────────────
    gt_limit = global_tool_limit or _GLOBAL_TOOL_RUN_LIMIT
    middleware.append(
        ToolCallLimitMiddleware(
            run_limit=gt_limit,
            exit_behavior="end",
        )
    )
    logger.info(
        "Middleware: global tool limit",
        extra={"run_limit": gt_limit},
    )

    # ── 3. Model call guard ───────────────────────────────────────────────
    mc_limit = model_call_limit or _MODEL_CALL_RUN_LIMIT
    middleware.append(
        ModelCallLimitMiddleware(
            run_limit=mc_limit,
            exit_behavior="end",  # Graceful stop instead of exception
        )
    )
    logger.info(
        "Middleware: model call limit",
        extra={"run_limit": mc_limit},
    )

    # ── 4. Tool retry (transient failures) ────────────────────────────────
    should_retry = enable_tool_retry if enable_tool_retry is not None else _ENABLE_TOOL_RETRY
    if should_retry:
        middleware.append(
            ToolRetryMiddleware(
                max_retries=2,
                backoff_factor=1.5,
                initial_delay=0.5,
                max_delay=10.0,
                on_failure="continue",  # Let LLM see the error as a ToolMessage
            )
        )
        logger.info("Middleware: tool retry enabled")

    # ── 5. Extra (caller-provided) ────────────────────────────────────────
    if extra_middleware:
        middleware.extend(extra_middleware)
        logger.info(
            "Middleware: extra appended",
            extra={"count": len(extra_middleware)},
        )

    logger.info(
        "Middleware stack assembled",
        extra={
            "total_middleware": len(middleware),
            "types": [type(m).__name__ for m in middleware],
        },
    )

    return middleware
