"""Shell allow list middleware — validates command tools for safety."""

from collections.abc import Callable
from k8s_autopilot.utils.logger import AgentLogger
from typing import Any

from langchain_core.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware

logger = AgentLogger("ShellAllowList")


def is_shell_command_allowed(command: str, allow_list: list[str]) -> bool:
    """Check if the given shell command's binary is in the allow-list."""
    parts = command.strip().split()
    if not parts:
        return False
    binary = parts[0]
    return binary in allow_list


@register_middleware(name="shell_allow_list")
class ShellAllowListMiddleware(BaseAgentMiddleware):
    """Validate shell commands against an allow-list without Graph interruptions."""

    def __init__(self, allow_list: list[str]) -> None:
        super().__init__()
        if not allow_list:
            raise ValueError("allow_list must not be empty")
        self._allow_list = list(allow_list)

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        """Return an error tool message when a shell command is not allowed."""
        if request.tool_call["name"] != "execute":
            return None

        args = request.tool_call.get("args") or {}
        command = args.get("command", "")
        if is_shell_command_allowed(command, self._allow_list):
            logger.debug(f"Shell command allowed: {command!r}")
            return None

        logger.warning(f"Shell command rejected by allow-list: {command!r}")
        allowed_str = ", ".join(self._allow_list)
        return ToolMessage(
            content=(
                f"Shell command rejected: `{command}` is not in the allow-list. "
                f"Allowed commands: {allowed_str}. "
                f"Please use an allowed command or try another approach."
            ),
            name="execute",
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        rejected = self._validate_tool_call(request)
        if rejected is not None:
            return rejected
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage | Command[Any]:
        rejected = self._validate_tool_call(request)
        if rejected is not None:
            return rejected
        return await handler(request)
