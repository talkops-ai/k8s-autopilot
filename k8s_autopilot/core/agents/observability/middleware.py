"""
Observability Operator — Modular middleware factory for deep agent safety nets.

Provides a configurable factory that assembles middleware stacks for the
ObservabilityCoordinator deep agent.  Each middleware is independently
toggleable via ``Config`` or environment variables.

Usage::

    from k8s_autopilot.core.agents.observability.middleware import (
        build_obs_operator_middleware,
    )

    middleware = build_obs_operator_middleware(config)
    agent = create_deep_agent(
        ...,
        middleware=middleware,
    )
"""
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from deepagents.middleware.summarization import create_summarization_tool_middleware
from langchain.agents.middleware import HumanInTheLoopMiddleware, AgentMiddleware, AgentState
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

from langchain_core.messages import SystemMessage
from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("ObsOperatorMiddleware")


# ---------------------------------------------------------------------------
# Layer 1: ObsOperationContextMiddleware — survives summarization
# ---------------------------------------------------------------------------

class ObsOperationContextMiddleware(AgentMiddleware):
    """Injects recent observability operations context before every model call.

    Reads ``/memories/observability/operations-log.md`` from the agent's
    ``state["files"]`` and prepends a compact SystemMessage with recent
    operation details.  This context survives built-in summarization.
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Read operations journal and inject as SystemMessage."""
        from k8s_autopilot.utils.operations_context import (
            get_obs_operations_context_from_state,
        )

        ops_context = get_obs_operations_context_from_state(dict(state))
        if not ops_context:
            return None

        logger.debug(
            "ObsOperationContextMiddleware: injecting operations context",
            extra={"context_length": len(ops_context)},
        )

        return {
            "messages": [
                SystemMessage(
                    content=(
                        "## Active Observability Operations Context (auto-injected, "
                        "survives summarization)\n"
                        "The following observability operations were performed in "
                        "this session. Use this context for ANY follow-up "
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
# Default limits (overridable via env vars or Config)
# ---------------------------------------------------------------------------

_WRITE_FILE_RUN_LIMIT = int(os.getenv("OBS_OP_WRITE_FILE_RUN_LIMIT", "20"))
_GLOBAL_TOOL_RUN_LIMIT = int(os.getenv("OBS_OP_GLOBAL_TOOL_RUN_LIMIT", "60"))
_MODEL_CALL_RUN_LIMIT = int(os.getenv("OBS_OP_MODEL_CALL_RUN_LIMIT", "40"))
_ENABLE_TOOL_RETRY = os.getenv("OBS_OP_ENABLE_TOOL_RETRY", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Prometheus HITL — approval card descriptions
# ---------------------------------------------------------------------------

def _build_prometheus_approval_description(
    tool_name: str, tool_args: Dict[str, Any]
) -> str:
    """Build a rich, human-readable description for Prometheus HITL cards.

    Namespace-aware, action-differentiated. Covers: exporter install/uninstall,
    ServiceMonitor creation, rule group upsert, file_sd management, and
    remote-write configuration.
    """

    ns = tool_args.get("namespace", "unknown")
    backend_id = tool_args.get("backend_id", "default")

    # -- Exporter Install ---------------------------------------------------
    if tool_name == "prom_install_exporter":
        exporter_type = tool_args.get("exporter_type", "unknown")
        return (
            f"📦 **EXPORTER INSTALLATION — APPROVAL REQUIRED**\n\n"
            f"**Exporter**: {exporter_type}\n"
            f"**Namespace**: {ns}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ This will create Kubernetes resources (Deployment/DaemonSet + "
            f"Service, possibly RBAC and ConfigMap) on the cluster.\n"
            f"The exporter will start scraping the target system immediately."
        )

    # -- Exporter Uninstall -------------------------------------------------
    elif tool_name == "prom_uninstall_exporter":
        exporter_type = tool_args.get("exporter_type", "unknown")
        return (
            f"🗑️ **EXPORTER REMOVAL — APPROVAL REQUIRED**\n\n"
            f"**Exporter**: {exporter_type}\n"
            f"**Namespace**: {ns}\n\n"
            f"⚠️ This removes the exporter Deployment/DaemonSet + Service. "
            f"Monitoring data for the target system will stop flowing. "
            f"Historical data in Prometheus is preserved."
        )

    # -- ServiceMonitor Apply -----------------------------------------------
    elif tool_name == "prom_apply_servicemonitor":
        service_name = tool_args.get("service_name", "unknown")
        scrape_interval = tool_args.get("scrape_interval", "30s")
        return (
            f"📡 **SERVICEMONITOR CREATION — APPROVAL REQUIRED**\n\n"
            f"**Service**: {service_name}\n"
            f"**Namespace**: {ns}\n"
            f"**Scrape Interval**: {scrape_interval}\n\n"
            f"⚠️ This creates a ServiceMonitor CRD that wires the service "
            f"to Prometheus. Requires Prometheus Operator to be installed."
        )

    # -- Rule Group Upsert --------------------------------------------------
    elif tool_name == "prom_upsert_rule_group":
        group_name = tool_args.get("group_name", "unknown")
        storage_mode = tool_args.get("storage_mode", "api")
        rule_count = len(tool_args.get("rules", []))
        return (
            f"📋 **RULE GROUP UPSERT — APPROVAL REQUIRED**\n\n"
            f"**Group**: {group_name}\n"
            f"**Backend**: {backend_id}\n"
            f"**Storage Mode**: {storage_mode}\n"
            f"**Rules**: {rule_count} rule(s)\n"
            f"**Namespace**: {ns}\n\n"
            f"⚠️ This creates or updates alerting/recording rules. "
            f"{'CRD mode will patch a Kubernetes PrometheusRule resource.' if storage_mode == 'k8s_crd' else 'API mode writes rules via the Prometheus Rules API.'}"
        )

    # -- File SD Management -------------------------------------------------
    elif tool_name == "prom_manage_file_sd":
        targets = tool_args.get("targets", [])
        file_sd_path = tool_args.get("file_sd_path", "unknown")
        sub_action = tool_args.get("sub_action", "add")
        return (
            f"📁 **FILE SERVICE DISCOVERY — APPROVAL REQUIRED**\n\n"
            f"**Action**: {sub_action}\n"
            f"**Targets**: {', '.join(targets) if targets else 'unknown'}\n"
            f"**File**: {file_sd_path}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ This modifies the file_sd targets JSON and may trigger "
            f"a Prometheus configuration reload."
        )

    # -- Remote Write Configuration -----------------------------------------
    elif tool_name == "prom_configure_remote_write":
        remote_url = tool_args.get("remote_url", "unknown")
        return (
            f"🔗 **REMOTE WRITE CONFIGURATION — APPROVAL REQUIRED**\n\n"
            f"**Remote URL**: {remote_url}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ This generates remote_write YAML configuration. "
            f"The output must be manually applied to the Prometheus config."
        )

    else:
        return f"Approval required for Prometheus operation: {tool_name}."


# ---------------------------------------------------------------------------
# Alertmanager HITL — approval card descriptions
# ---------------------------------------------------------------------------

def _build_alertmanager_approval_description(
    tool_name: str, tool_args: Dict[str, Any]
) -> str:
    """Build a rich, human-readable description for Alertmanager HITL cards.

    Covers: silence creation/update/expiration, test alert push, and
    quick-silence helper.
    """

    backend_id = tool_args.get("backend_id", "default")

    # -- Create Silence -----------------------------------------------------
    if tool_name == "am_create_silence":
        matchers = tool_args.get("matchers", [])
        duration = tool_args.get("duration_minutes", "?")
        comment = tool_args.get("comment", "")
        created_by = tool_args.get("created_by", "unknown")
        matcher_str = ", ".join(
            f"{m.get('name', '?')}={m.get('value', '?')}"
            for m in (matchers if isinstance(matchers, list) else [])
        ) or "unknown"
        return (
            f"🔇 **SILENCE CREATION — APPROVAL REQUIRED**\n\n"
            f"**Matchers**: {matcher_str}\n"
            f"**Duration**: {duration} minutes\n"
            f"**Created By**: {created_by}\n"
            f"**Comment**: {comment or 'n/a'}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ This will suppress notifications for matching alerts. "
            f"Critical alerts may go unnoticed during the silence window."
        )

    # -- Update Silence -----------------------------------------------------
    elif tool_name == "am_update_silence":
        silence_id = tool_args.get("silence_id", "unknown")
        add_minutes = tool_args.get("add_minutes", None)
        new_ends_at = tool_args.get("new_ends_at", None)
        extension = (
            f"{add_minutes} minutes" if add_minutes
            else f"until {new_ends_at}" if new_ends_at
            else "unknown duration"
        )
        return (
            f"🔄 **SILENCE EXTENSION — APPROVAL REQUIRED**\n\n"
            f"**Silence ID**: {silence_id}\n"
            f"**Extension**: {extension}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ This extends the silence window. Alerts will remain "
            f"suppressed for the additional duration."
        )

    # -- Expire Silence -----------------------------------------------------
    elif tool_name == "am_expire_silence":
        silence_id = tool_args.get("silence_id", "unknown")
        return (
            f"🔔 **SILENCE EXPIRATION — APPROVAL REQUIRED**\n\n"
            f"**Silence ID**: {silence_id}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚡ This reactivates alert notifications immediately. "
            f"Previously silenced alerts will start firing again."
        )

    # -- Push Test Alert ----------------------------------------------------
    elif tool_name == "am_push_test_alert":
        labels = tool_args.get("alert_labels", {})
        alertname = labels.get("alertname", "unknown") if isinstance(labels, dict) else "unknown"
        return (
            f"🧪 **TEST ALERT — APPROVAL REQUIRED**\n\n"
            f"**Alert Name**: {alertname}\n"
            f"**Labels**: {labels}\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ This fires a **REAL** alert into Alertmanager. "
            f"Downstream integrations (Slack, PagerDuty, email, webhooks) "
            f"**will receive notifications**."
        )

    # -- Silence Alert (helper) ---------------------------------------------
    elif tool_name == "am_silence_alert":
        scope = tool_args.get("scope", "service")
        duration = tool_args.get("duration_minutes", "?")
        fingerprint = tool_args.get("fingerprint", None)
        alert_labels = tool_args.get("alert_labels", {})
        identifier = fingerprint or str(alert_labels) or "unknown"
        return (
            f"🔇 **QUICK SILENCE — APPROVAL REQUIRED**\n\n"
            f"**Alert**: {identifier}\n"
            f"**Scope**: {scope}\n"
            f"**Duration**: {duration} minutes\n"
            f"**Backend**: {backend_id}\n\n"
            f"⚠️ Scope '{scope}' determines blast radius: "
            f"'instance' = narrowest, 'service' = recommended, "
            f"'env' = broadest."
        )

    else:
        return f"Approval required for Alertmanager operation: {tool_name}."


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

def build_prometheus_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Create a ``HumanInTheLoopMiddleware`` configured for Prometheus operations.

    Gated tools (state-modifying):
        - prom_install_exporter — creates K8s resources (Deployment + Service + RBAC)
        - prom_uninstall_exporter — removes K8s resources
        - prom_apply_servicemonitor — creates ServiceMonitor CRD
        - prom_upsert_rule_group — creates/modifies alerting/recording rules
        - prom_manage_file_sd — modifies file_sd targets + optional reload
        - prom_configure_remote_write — generates remote_write YAML
    """
    logger.info("Building HumanInTheLoopMiddleware for Prometheus execution tools")

    return HumanInTheLoopMiddleware(
        interrupt_on={
            "prom_install_exporter": _make_interrupt_config(
                "prom_install_exporter", _build_prometheus_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "prom_uninstall_exporter": _make_interrupt_config(
                "prom_uninstall_exporter", _build_prometheus_approval_description,
            ),
            "prom_apply_servicemonitor": _make_interrupt_config(
                "prom_apply_servicemonitor", _build_prometheus_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "prom_upsert_rule_group": _make_interrupt_config(
                "prom_upsert_rule_group", _build_prometheus_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "prom_manage_file_sd": _make_interrupt_config(
                "prom_manage_file_sd", _build_prometheus_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "prom_configure_remote_write": _make_interrupt_config(
                "prom_configure_remote_write", _build_prometheus_approval_description,
                allowed_decisions=["approve", "reject"],
            ),
        },
        description_prefix="⚠️ Prometheus Operation — Approval Required",
    )


def build_alertmanager_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Create a ``HumanInTheLoopMiddleware`` configured for Alertmanager operations.

    Gated tools (state-modifying):
        - am_create_silence — suppresses alert notifications
        - am_update_silence — extends silence duration
        - am_expire_silence — reactivates notifications
        - am_push_test_alert — fires real alert to downstream integrations
        - am_silence_alert — quick-silence helper (creates silence from alert)
    """
    logger.info("Building HumanInTheLoopMiddleware for Alertmanager execution tools")

    return HumanInTheLoopMiddleware(
        interrupt_on={
            "am_create_silence": _make_interrupt_config(
                "am_create_silence", _build_alertmanager_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "am_update_silence": _make_interrupt_config(
                "am_update_silence", _build_alertmanager_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
            "am_expire_silence": _make_interrupt_config(
                "am_expire_silence", _build_alertmanager_approval_description,
            ),
            "am_push_test_alert": _make_interrupt_config(
                "am_push_test_alert", _build_alertmanager_approval_description,
            ),
            "am_silence_alert": _make_interrupt_config(
                "am_silence_alert", _build_alertmanager_approval_description,
                allowed_decisions=["approve", "edit", "reject"],
            ),
        },
        description_prefix="⚠️ Alertmanager Operation — Approval Required",
    )


# ---------------------------------------------------------------------------
# Coordinator middleware stack
# ---------------------------------------------------------------------------

def build_obs_operator_middleware(
    config: Optional["Config"] = None,
    *,
    write_file_limit: Optional[int] = None,
    global_tool_limit: Optional[int] = None,
    model_call_limit: Optional[int] = None,
    enable_tool_retry: Optional[bool] = None,
    extra_middleware: Optional[List[Any]] = None,
    model: Optional[str] = None,
    backend: Optional[Any] = None,
) -> List[Any]:
    """Assemble the middleware stack for the ObservabilityCoordinator deep agent.

    Layers:
        0. ObsOperationContextMiddleware — injects recent ops context
        0b. PlanLockMiddleware — re-injects approved plan constraints
        1. ToolCallLimitMiddleware (write_file) — prevent runaway writes
        2. ToolCallLimitMiddleware (global) — hard cap on total tool calls
        3. ModelCallLimitMiddleware — hard cap on LLM calls
        4. ToolRetryMiddleware (optional) — transient failure recovery
        5. SummarizationToolMiddleware — proactive context compression
        6. Extra middleware (caller-supplied)
    """
    from langchain.agents.middleware import (
        ToolCallLimitMiddleware,
        ModelCallLimitMiddleware,
        ToolRetryMiddleware,
    )
    from k8s_autopilot.core.agents.app_operator.middleware import PlanLockMiddleware

    middleware: List[Any] = []

    # 0. Operation context injection
    middleware.append(ObsOperationContextMiddleware())
    logger.info("Middleware: ObsOperationContextMiddleware (before_model)")

    # 0b. Plan lock enforcement (re-injects approved plan before every model call)
    middleware.append(PlanLockMiddleware())
    logger.info("Middleware: PlanLockMiddleware (before_model)")

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

    # 5. Summarization tool — lets the coordinator proactively compress its
    #    message history between task delegations (after request_chat_continue)
    #    rather than waiting until the automatic 85%-threshold reactive
    #    summarization, which can cause mid-generation token overflow crashes.
    _model = model or (config.get_llm_config().get("model") if config else None)
    _backend = backend
    if _model and _backend:
        try:
            middleware.append(
                create_summarization_tool_middleware(_model, _backend)
            )
            logger.info("Middleware: SummarizationToolMiddleware added")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SummarizationToolMiddleware unavailable — skipping",
                extra={"error": str(exc)},
            )

    # 6. Extra middleware
    if extra_middleware:
        middleware.extend(extra_middleware)

    return middleware
