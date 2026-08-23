"""Operation context injection middlewares."""

from typing import Any, Dict, Optional
from langchain_core.messages import SystemMessage
from langchain.agents.middleware import AgentState

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("OperationContext")


@register_middleware(name="operation_context")
class OperationContextMiddleware(BaseAgentMiddleware):
    """Generic middleware that injects recent operations context from a markdown log.

    Reads a specified operations journal file from the agent's virtual
    ``state["files"]`` and prepends a SystemMessage.
    """

    def __init__(
        self,
        log_path: str,
        prefix: str,
        header: str = "Active Operations Context",
        instructions: str = (
            "The following operations were performed in this session. "
            "Use this context for follow-up requests. Do NOT re-ask the "
            "user for details already listed here."
        )
    ) -> None:
        super().__init__()
        self.log_path = log_path
        self.prefix = prefix
        self.header = header
        self.instructions = instructions

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Read operations journal and inject as SystemMessage."""
        from k8s_autopilot.utils.operations_context import _get_context_from_state

        ops_context = _get_context_from_state(dict(state), self.log_path, self.prefix)
        if not ops_context:
            return None

        logger.debug(
            f"OperationContextMiddleware ({self.prefix}): injecting operations context",
            extra={"context_length": len(ops_context)},
        )

        return {
            "messages": [
                SystemMessage(
                    content=(
                        f"## {self.header} (auto-injected, survives summarization)\n"
                        f"{self.instructions}\n\n"
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


@register_middleware(name="supervisor_context")
class SupervisorContextMiddleware(BaseAgentMiddleware):
    """Re-injects cross-domain context before every supervisor model call.

    Reads ``domain_summaries`` from ``MainSupervisorState`` and prepends a
    compact SystemMessage so the supervisor always has awareness of what
    each coordinator accomplished — even after older messages are summarized.
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Read domain summaries from state and inject as SystemMessage."""
        raw = state.get("domain_summaries", [])
        domain_summaries = raw if isinstance(raw, list) else []

        if not domain_summaries:
            return None

        lines: list[str] = []
        for summary in domain_summaries:
            if not isinstance(summary, dict):
                continue
            domain = summary.get("domain", "unknown")
            outcome = summary.get("outcome", "completed")
            detail = summary.get("detail", "")
            if detail:
                lines.append(f"- **{domain}**: {outcome} — {detail}")
            else:
                lines.append(f"- **{domain}**: {outcome}")

        if not lines:
            return None

        context_text = (
            "## Cross-Domain Context (auto-injected, survives "
            "summarization)\n"
            "Previous coordinator outcomes this session:\n"
            + "\n".join(lines)
            + "\n\nUse this context when routing follow-up requests."
        )

        logger.debug(
            "SupervisorContextMiddleware: injecting domain summaries",
            extra={
                "summary_count": len(lines),
                "context_length": len(context_text),
            },
        )

        return {
            "messages": [SystemMessage(content=context_text)],
        }

    async def abefore_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Async version — delegates to sync implementation."""
        return self.before_model(state, runtime)
