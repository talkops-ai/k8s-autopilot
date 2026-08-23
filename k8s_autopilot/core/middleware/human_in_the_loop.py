import logging
from typing import Any, Dict, List, cast
from langchain.agents.middleware import HumanInTheLoopMiddleware as LCHumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig, DecisionType
from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.core.agents.helm_operator.middleware import _build_approval_description

logger = logging.getLogger("k8s_autopilot")

@register_middleware(name="human_in_the_loop")
class HumanInTheLoopMiddleware(BaseAgentMiddleware):
    """Declarative human-in-the-loop (HITL) middleware wrapper for LangGraph agents."""

    def __init__(self, *, tools: List[Any], **kwargs: Any) -> None:
        super().__init__()
        interrupt_on: Dict[str, bool | InterruptOnConfig] = {}
        for tool_item in tools:
            if isinstance(tool_item, dict):
                tool_name = tool_item.get("name")
                decisions = tool_item.get("decisions", ["approve", "reject"])
            else:
                tool_name = tool_item
                decisions = ["approve", "reject"]
            
            if tool_name:
                interrupt_on[tool_name] = InterruptOnConfig(
                    allowed_decisions=cast(List[DecisionType], decisions),
                    description=lambda tool_call, state, runtime, tn=tool_name: _build_approval_description(
                        tn, tool_call.get("args", {}),
                    ),
                )
        self._wrapped = LCHumanInTheLoopMiddleware(interrupt_on=interrupt_on)

    def wrap_model_call(self, *args: Any, **kwargs: Any) -> Any:
        return self._wrapped.wrap_model_call(*args, **kwargs)
    
    def wrap_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        return self._wrapped.wrap_tool_call(*args, **kwargs)
