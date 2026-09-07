"""Human-in-the-loop middleware for interactive approval workflows."""

from typing import Any, cast

from langchain.agents.middleware import (
    HumanInTheLoopMiddleware as LCHumanInTheLoopMiddleware,
)
from langchain.agents.middleware.human_in_the_loop import (
    DecisionType,
    InterruptOnConfig,
)

from k8s_autopilot.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


def _build_approval_description(tool_name: str, args: dict[str, Any]) -> str:
    return f"Approval required for {tool_name} with args: {args}"


@register_middleware(name="human_in_the_loop")
class HumanInTheLoopMiddleware(BaseAgentMiddleware):
    """Declarative human-in-the-loop (HITL) middleware wrapper for LangGraph agents."""

    def __init__(self, *, tools: list[Any], **kwargs: Any) -> None:
        """Initialize HumanInTheLoopMiddleware with tools requiring human approval.

        Args:
            tools: List of tool names or configuration dicts specifying required decisions.
            **kwargs: Additional keyword arguments passed to base middleware.
        """
        super().__init__()
        interrupt_on: dict[str, bool | InterruptOnConfig] = {}
        for tool_item in tools:
            if isinstance(tool_item, dict):
                tool_name = tool_item.get("name")
                decisions = tool_item.get("decisions", ["approve", "reject"])
            else:
                tool_name = tool_item
                decisions = ["approve", "reject"]

            if tool_name:
                interrupt_on[tool_name] = InterruptOnConfig(
                    allowed_decisions=cast(list[DecisionType], decisions),
                    description=lambda tool_call, state, runtime, tn=tool_name: _build_approval_description(
                        tn,
                        tool_call.get("args", {}),
                    ),
                )
        self._wrapped = LCHumanInTheLoopMiddleware(interrupt_on=interrupt_on)

    def wrap_model_call(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate model call wrapping to the underlying LangChain HITL middleware."""
        return self._wrapped.wrap_model_call(*args, **kwargs)

    def wrap_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate tool call wrapping to the underlying LangChain HITL middleware."""
        return self._wrapped.wrap_tool_call(*args, **kwargs)
