"""Plan lock middleware — enforces plan→execute fidelity."""

from typing import Any, Dict, Optional
from langchain_core.messages import SystemMessage
from langchain.agents.middleware import AgentState

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("PlanLockMiddleware")


@register_middleware(name="plan_lock")
class PlanLockMiddleware(BaseAgentMiddleware):
    """Re-injects the approved plan as a binding constraint before every model call.

    Reads from two sources (checked in order):
    1. state["todos"] - the native Deep Agent TodoListMiddleware channel.
    2. state["files"]["/plan/active-plan.md"] - legacy fallback.
    """

    _PLAN_PATH = "/plan/active-plan.md"

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Re-inject approved plan as a binding constraint."""
        todos = state.get("todos") or []

        if isinstance(todos, list) and todos:
            return self._build_todos_constraint(todos, state)

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

    @staticmethod
    def _build_todos_constraint(
        todos: list, state: AgentState,
    ) -> Optional[Dict[str, Any]]:
        """Serialise ``state["todos"]`` as a binding SystemMessage."""
        non_completed = []
        completed = []
        for todo in todos:
            status = (
                todo.get("status", "pending")
                if isinstance(todo, dict)
                else getattr(todo, "status", "pending")
            )
            title = (
                todo.get("title") or todo.get("content") or "Untitled"
                if isinstance(todo, dict)
                else getattr(todo, "title", getattr(todo, "content", "Untitled"))
            )
            if status in ("completed", "failed", "skipped"):
                completed.append(f"  ✅ {title} ({status})")
            else:
                non_completed.append(f"  ⏳ {title} ({status})")

        if not non_completed:
            logger.debug(
                "PlanLockMiddleware: all todos completed, no constraint needed",
                extra={"completed_count": len(completed)},
            )
            return None

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

    @classmethod
    def _get_active_plan_from_files(cls, state: AgentState) -> Optional[str]:
        """Extract active plan content from state files (backward compat)."""
        files = state.get("files", {})
        if not isinstance(files, dict) or not files:
            return None

        plan_file = files.get(cls._PLAN_PATH)
        if plan_file is None:
            return None

        if isinstance(plan_file, str):
            return plan_file.strip() or None
        if isinstance(plan_file, dict):
            content = plan_file.get("content", "")
            return content.strip() or None

        return None
