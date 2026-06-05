"""A2UI v0.9 surface operations builder.

Provides a clean, type-safe API for constructing A2UI surfaces
following the standard lifecycle:

    createSurface → updateComponents → updateDataModel

Each ``build_*_surface()`` function returns a list of A2UI operations
ready to be wrapped via ``create_a2ui_part()`` and emitted as A2A Parts.

The ``update_*_data()`` functions return a single ``updateDataModel``
operation for patching an existing surface (e.g., updating tool status
from ``running`` → ``success``).

Custom components used here (``toolExecutionCard``, ``thoughtBlock``,
``hitlApprovalCard``) are registered on the UI-side Lit component
registry. Property names match the ``@property`` decorators on the
corresponding Lit elements exactly.

Reference:
    CopilotKit A2UI docs: https://docs.copilotkit.ai/integrations/langgraph/generative-ui/a2ui
    UI components: talkops-ui/src/custom-components/
"""

from __future__ import annotations

from typing import Any

from copilotkit import a2ui

from k8s_autopilot.core.a2ui.risk_classification import RiskLevel

# ── Catalog constants ─────────────────────────────────────────────────────

TALKOPS_CATALOG_ID = "talkops-reasoning-catalog"


# ── toolExecutionCard ─────────────────────────────────────────────────────

def build_tool_execution_surface(
    surface_id: str,
    tool_name: str,
    status: str = "pending",
    parameters: dict[str, Any] | None = None,
    terminal_output: str = "",
    environment: str = "",
    duration_ms: int = 0,
) -> list[dict[str, Any]]:
    """Build a ``toolExecutionCard`` surface.

    Emits the full lifecycle: createSurface → updateComponents → updateDataModel.

    Args:
        surface_id: Unique surface identifier (e.g., ``tool-kubectl_apply-a1b2c3d4``).
        tool_name: Name of the tool being executed.
        status: Execution status (``pending`` | ``running`` | ``success`` | ``error``).
        parameters: Tool input parameters dict (displayed in expandable section).
        terminal_output: Tool stdout/stderr output.
        environment: Target environment label (e.g., cluster name).
        duration_ms: Execution duration in milliseconds.

    Returns:
        List of A2UI operation dicts.
    """
    return [
        a2ui.create_surface(surface_id, catalog_id=TALKOPS_CATALOG_ID),
        a2ui.update_components(surface_id, [
            {
                "id": "root",
                "component": {
                    "toolExecutionCard": {
                        "toolName": {"path": "/toolName"},
                        "status": {"path": "/status"},
                        "parameters": {"path": "/parameters"},
                        "terminalOutput": {"path": "/terminalOutput"},
                        "environment": {"path": "/environment"},
                        "durationMs": {"path": "/durationMs"},
                    }
                },
            },
        ]),
        a2ui.update_data_model(surface_id, {
            "toolName": tool_name,
            "status": status,
            "parameters": parameters or {},
            "terminalOutput": terminal_output,
            "environment": environment,
            "durationMs": duration_ms,
        }),
    ]


def update_tool_execution_data(
    surface_id: str,
    **updates: Any,
) -> dict[str, Any]:
    """Build an ``updateDataModel`` operation for an existing tool surface.

    Use to patch fields without re-emitting createSurface + updateComponents.

    Args:
        surface_id: The surface to update.
        **updates: Field-value pairs to update (e.g., ``status="success"``).

    Returns:
        A single ``updateDataModel`` operation dict.
    """
    return a2ui.update_data_model(surface_id, updates)


# ── thoughtBlock ──────────────────────────────────────────────────────────

def build_thought_block_surface(
    surface_id: str,
    title: str = "",
    summary: str = "",
    severity: str = "info",
) -> list[dict[str, Any]]:
    """Build a ``thoughtBlock`` surface for reasoning token display.

    Args:
        surface_id: Unique surface identifier (e.g., ``thought-a1b2c3d4``).
        title: Agent or node label (e.g., ``"Supervisor"``).
        summary: Reasoning text content (last N chars for streaming).
        severity: Thought severity (``info`` | ``warning`` | ``error``).

    Returns:
        List of A2UI operation dicts.
    """
    return [
        a2ui.create_surface(surface_id, catalog_id=TALKOPS_CATALOG_ID),
        a2ui.update_components(surface_id, [
            {
                "id": "root",
                "component": {
                    "thoughtBlock": {
                        "title": {"path": "/title"},
                        "summary": {"path": "/summary"},
                        "severity": {"path": "/severity"},
                    }
                },
            },
        ]),
        a2ui.update_data_model(surface_id, {
            "title": title,
            "summary": summary,
            "severity": severity,
        }),
    ]


def update_thought_block_data(
    surface_id: str,
    **updates: Any,
) -> dict[str, Any]:
    """Build an ``updateDataModel`` operation for an existing thought surface.

    Args:
        surface_id: The surface to update.
        **updates: Field-value pairs to update (e.g., ``summary="new text"``).

    Returns:
        A single ``updateDataModel`` operation dict.
    """
    return a2ui.update_data_model(surface_id, updates)


# ── hitlApprovalCard ──────────────────────────────────────────────────────

def build_hitl_approval_surface(
    surface_id: str,
    proposed_action: str,
    justification: str,
    risk_level: str | RiskLevel = "medium",
    options: list[dict[str, str]] | None = None,
    action_id: str = "hitl_response",
    phase: str = "unknown",
    parameters: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build a ``hitlApprovalCard`` surface for HITL decision points.

    The custom component dispatches ``CustomEvent("a2uiaction")`` with
    ``{actionId, value: {choice, label, riskLevel, ...}}``, which flows
    through the AG-UI bridge to the agent as a ``userAction`` DataPart.

    Additionally, standard A2UI ``Button`` action components are included
    as fallback for standard A2UI renderers.

    Args:
        surface_id: Unique surface identifier (e.g., ``hitl-helm_operation-a1b2c3d4``).
        proposed_action: Human-readable description of the proposed action.
        justification: Detailed context / evidence for the approval request.
        risk_level: Risk classification (``low`` | ``medium`` | ``high`` | ``critical``).
        options: List of decision options. Each is ``{"id": ..., "label": ...}``.
            Defaults to Approve/Edit/Reject.
        action_id: Action name for the HITL response dispatch.
            Defaults to ``hitl_response`` (backward-compatible).
        phase: Workflow phase identifier for context tracking.
        parameters: Optional list of ``{"key": "Label", "value": "display_value"}``
            dicts extracted from ``action_requests``. Rendered as a read-only
            parameter grid in the frontend ``hitlApprovalCard``.

    Returns:
        List of A2UI operation dicts.
    """
    if options is None:
        options = [
            {"id": "approve", "label": "✅ Approve"},
            {"id": "edit", "label": "✏️ Edit"},
            {"id": "reject", "label": "❌ Reject"},
        ]

    risk_str = str(risk_level)

    return [
        a2ui.create_surface(surface_id, catalog_id=TALKOPS_CATALOG_ID),
        a2ui.update_components(surface_id, [
            {
                "id": "root",
                "component": {
                    "hitlApprovalCard": {
                        "proposedAction": {"path": "/proposedAction"},
                        "justification": {"path": "/justification"},
                        "riskLevel": {"path": "/riskLevel"},
                        "options": {"path": "/options"},
                        "onDecisionActionId": {"path": "/onDecisionActionId"},
                        "parameters": {"path": "/parameters"},
                    }
                },
            },
        ]),
        a2ui.update_data_model(surface_id, {
            "proposedAction": proposed_action,
            "justification": justification,
            "riskLevel": risk_str,
            "options": options,
            "onDecisionActionId": action_id,
            "phaseId": phase,
            "parameters": parameters or [],
        }),
    ]


# ── planTodoList ──────────────────────────────────────────────────────────

def build_plan_todo_surface(
    surface_id: str,
    todos: list[dict[str, Any]],
    plan_title: str = "Execution Plan",
    plan_version: int = 1,
    coordinator: str = "",
) -> list[dict[str, Any]]:
    """Build a ``planTodoList`` surface for real-time TODO tracking.

    Emits the full lifecycle: createSurface → updateComponents → updateDataModel.

    Args:
        surface_id: Unique surface identifier (e.g., ``plan-todo-app-operator-a1b2``).
        todos: List of TODO items, each with ``title``, ``status``, ``content``.
        plan_title: Header title for the plan card.
        plan_version: Plan version number (shown as vN badge if > 1).
        coordinator: Coordinator name for context.

    Returns:
        List of A2UI operation dicts.
    """
    return [
        a2ui.create_surface(surface_id, catalog_id=TALKOPS_CATALOG_ID),
        a2ui.update_components(surface_id, [
            {
                "id": "root",
                "component": {
                    "planTodoList": {
                        "todos": {"path": "/todos"},
                        "planTitle": {"path": "/planTitle"},
                        "planVersion": {"path": "/planVersion"},
                        "coordinator": {"path": "/coordinator"},
                    }
                },
            },
        ]),
        a2ui.update_data_model(surface_id, {
            "todos": todos,
            "planTitle": plan_title,
            "planVersion": plan_version,
            "coordinator": coordinator,
        }),
    ]


def update_plan_todo_data(
    surface_id: str,
    **updates: Any,
) -> dict[str, Any]:
    """Build an ``updateDataModel`` operation for an existing plan-todo surface.

    Use to patch the todos array (e.g., update status from pending → in_progress)
    without re-emitting the full surface lifecycle.

    Args:
        surface_id: The surface to update.
        **updates: Field-value pairs to update (e.g., ``todos=[...]``).

    Returns:
        A single ``updateDataModel`` operation dict.
    """
    return a2ui.update_data_model(surface_id, updates)


# ── executionWalkthrough ──────────────────────────────────────────────────

def build_walkthrough_surface(
    surface_id: str,
    walkthrough: str,
    coordinator: str = "",
    status: str = "success",
    total_tasks: int = 0,
    completed_tasks: int = 0,
) -> list[dict[str, Any]]:
    """Build an ``executionWalkthrough`` surface for post-execution narrative.

    Emits the full lifecycle: createSurface → updateComponents → updateDataModel.

    Args:
        surface_id: Unique surface identifier (e.g., ``walkthrough-app-operator-a1b2``).
        walkthrough: Walkthrough narrative text (markdown-like formatting supported).
        coordinator: Coordinator name for context badge.
        status: Overall execution outcome (``success`` | ``partial`` | ``failed``).
        total_tasks: Total number of planned tasks.
        completed_tasks: Number of successfully completed tasks.

    Returns:
        List of A2UI operation dicts.
    """
    return [
        a2ui.create_surface(surface_id, catalog_id=TALKOPS_CATALOG_ID),
        a2ui.update_components(surface_id, [
            {
                "id": "root",
                "component": {
                    "executionWalkthrough": {
                        "walkthrough": {"path": "/walkthrough"},
                        "coordinator": {"path": "/coordinator"},
                        "status": {"path": "/status"},
                        "totalTasks": {"path": "/totalTasks"},
                        "completedTasks": {"path": "/completedTasks"},
                    }
                },
            },
        ]),
        a2ui.update_data_model(surface_id, {
            "walkthrough": walkthrough,
            "coordinator": coordinator,
            "status": status,
            "totalTasks": total_tasks,
            "completedTasks": completed_tasks,
        }),
    ]


def update_walkthrough_data(
    surface_id: str,
    **updates: Any,
) -> dict[str, Any]:
    """Build an ``updateDataModel`` operation for an existing walkthrough surface.

    Args:
        surface_id: The surface to update.
        **updates: Field-value pairs to update (e.g., ``walkthrough="new text"``).

    Returns:
        A single ``updateDataModel`` operation dict.
    """
    return a2ui.update_data_model(surface_id, updates)

