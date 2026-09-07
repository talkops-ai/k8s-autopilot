"""Server-side helpers for drafting acceptance criteria from goal objectives.

``GoalCriteriaMiddleware`` runs before the normal agent loop: when
``goal_criteria_request`` is present in graph state it invokes a nested
criteria agent (context-enabled with repository/web/MCP tools, plus a
goal-only fallback), extracts a ``GoalProposal``, and writes the pending
channels so the client/TUI can present the proposal for review. The middleware
then jumps to ``"end"`` so the main agent does not run.

Budget-limiting middleware classes (``_RepositoryToolBudgetMiddleware``,
``_WebSearchBudgetMiddleware``, ``_CriteriaContextBudgetMiddleware``,
``_GoalContextFallbackMiddleware``) are also defined here and passed as
middleware to the nested criteria agent graphs.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NotRequired, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    OmitFromOutput,
    hook_config,
)
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    ToolCall,
    ToolMessage,
    get_buffer_string,
)
from langgraph.errors import GraphRecursionError
from typing_extensions import TypedDict, override

from k8s_autopilot.middleware._repository_bounds import (
    REPOSITORY_GREP_MATCH_LIMIT as _REPOSITORY_GREP_MATCH_LIMIT,
    REPOSITORY_TOOL_CALL_LIMIT as _REPOSITORY_TOOL_CALL_LIMIT,
    REPOSITORY_TOOL_NAMES as _REPOSITORY_TOOL_NAMES,
    RepositoryBounds,
)
from k8s_autopilot.middleware.goal_state_notice import is_conversation_control_message
from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.middleware.resume_state import ResumeState
from k8s_autopilot.rubrics.generator import (
    GOAL_AMENDMENT_SYSTEM_PROMPT,
    GOAL_RUBRIC_SYSTEM_PROMPT,
    _goal_amendment_human_prompt,
    _goal_rubric_human_prompt,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from deepagents.backends.protocol import BackendProtocol
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.runtime import Runtime
    from langgraph.types import Command
    from k8s_autopilot.subagents.types import SubagentMetadata

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Budget constants
# ---------------------------------------------------------------------------

_REPOSITORY_RECURSION_LIMIT = _REPOSITORY_TOOL_CALL_LIMIT * 2 + 2
_REPOSITORY_OPERATION_BUDGET_CACHE_LIMIT = 128
_STRUCTURED_OUTPUT_TOOL_NAME = "GoalProposal"
_WEB_SEARCH_CALL_LIMIT = 3
_CONVERSATION_CONTEXT_MESSAGE_LIMIT = 8
_CONVERSATION_CONTEXT_MESSAGE_TEXT_LIMIT = 1_600
_CONVERSATION_CONTEXT_TOTAL_TEXT_LIMIT = 6_000
_CONVERSATION_CONTEXT_SERIALIZED_LIMIT = 12_000
_CRITERIA_CONTEXT_TOTAL_TEXT_LIMIT = 32_000
_CRITERIA_OBJECTIVE_DISPLAY_LIMIT = 160
_CRITERIA_RESULT_LOG_LIMIT = 500
_FALLBACK_RECURSION_LIMIT = 8

_CRITERIA_FALLBACK_ERRORS: tuple[type[BaseException], ...] = (
    GraphRecursionError,
    NotImplementedError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class GoalProposal(TypedDict):
    """Structured proposal returned by the criteria agent."""

    objective: str
    criteria: str


class _GoalCriteriaRequestBase(TypedDict):
    """Fields shared by every goal-criteria request."""

    request_id: str
    objective: str


class GoalCreateRequest(_GoalCriteriaRequestBase):
    """A new proposal or a rejection-based regeneration."""

    kind: Literal["create"]
    feedback: NotRequired[str]
    previous_criteria: NotRequired[str]


class GoalAmendRequest(_GoalCriteriaRequestBase):
    """An amendment to an accepted goal."""

    kind: Literal["amend"]
    criteria: str
    feedback: str


GoalCriteriaRequest = GoalCreateRequest | GoalAmendRequest


class GoalCriteriaState(ResumeState):
    """Main-agent state carrying a criteria request until it is cleared."""

    goal_criteria_request: NotRequired[
        Annotated[GoalCriteriaRequest | None, OmitFromOutput]
    ]


from deepagents.middleware.skills import SkillsState
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    OmitFromOutput,
    PrivateStateAttr,
    hook_config,
)


class GoalCriteriaAgentState(SkillsState):
    """Private per-invocation state for the nested criteria agent."""

    criteria_objective: NotRequired[str]
    criteria_operation_id: NotRequired[str]
    _subagent_registry: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


class _GoalContextFallbackMiddleware(AgentMiddleware[Any, Any]):
    """Retry a failed context-enabled model call without context tools."""

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        try:
            return handler(request)
        except Exception as first_error:
            logger.warning(
                "Criteria context model call failed; retrying from the goal alone",
                exc_info=True,
            )
            try:
                return handler(
                    request.override(
                        messages=_goal_only_messages(request.messages),
                        tools=[],
                    )
                )
            except Exception:
                logger.warning(
                    "Criteria goal-only fallback also failed", exc_info=True
                )
                raise first_error from None

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        try:
            return await handler(request)
        except Exception as first_error:
            logger.warning(
                "Criteria context model call failed; retrying from the goal alone",
                exc_info=True,
            )
            try:
                return await handler(
                    request.override(
                        messages=_goal_only_messages(request.messages),
                        tools=[],
                    )
                )
            except Exception:
                logger.warning(
                    "Criteria goal-only fallback also failed", exc_info=True
                )
                raise first_error from None


def _goal_only_messages(messages: Sequence[BaseMessage]) -> list[AnyMessage]:
    for message in messages:
        if isinstance(message, HumanMessage):
            return [message]
    return []


class _CriteriaContextBudgetMiddleware(
    AgentMiddleware[GoalCriteriaAgentState, None]
):
    """Bound tool-result text accumulated by one nested context operation."""

    def __init__(self, *, label: str = "Criteria context") -> None:
        super().__init__()
        self._label = label
        self._remaining: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def _take(self, request: ToolCallRequest, size: int) -> int:
        key = _RepositoryToolBudgetMiddleware._operation_key(request)
        with self._lock:
            remaining = self._remaining.get(key, _CRITERIA_CONTEXT_TOTAL_TEXT_LIMIT)
            allowed = min(size, remaining)
            self._remaining[key] = remaining - allowed
            self._remaining.move_to_end(key)
            while len(self._remaining) > _REPOSITORY_OPERATION_BUDGET_CACHE_LIMIT:
                self._remaining.popitem(last=False)
        return allowed

    def _bound_result(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        if not isinstance(result, ToolMessage):
            return result

        content = str(result.content or "")
        allowed = self._take(request, len(content))
        if allowed == len(content):
            bounded = content
        elif allowed == 0:
            bounded = ""
        else:
            marker = f"\n[{self._label} limit reached; additional content omitted.]"
            if allowed <= len(marker):
                bounded = marker[:allowed]
            else:
                bounded = content[: allowed - len(marker)] + marker
        return result.model_copy(update={"content": bounded})

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._bound_result(request, handler(request))

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest], Awaitable[ToolMessage | Command[Any]]
        ],
    ) -> ToolMessage | Command[Any]:
        return self._bound_result(request, await handler(request))


class _ContextToolCallBudgetMiddleware(AgentMiddleware[Any, Any]):
    """Bound selected context-tool calls independently for each nested operation."""

    def __init__(self, tool_names: set[str], *, limit: int) -> None:
        super().__init__()
        self._tool_names = frozenset(tool_names)
        self._limit = limit
        self._calls: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def _reserve(self, request: ToolCallRequest) -> bool:
        key = _RepositoryToolBudgetMiddleware._operation_key(request)
        with self._lock:
            count = self._calls.get(key, 0)
            if count >= self._limit:
                return False
            self._calls[key] = count + 1
            self._calls.move_to_end(key)
            while len(self._calls) > _REPOSITORY_OPERATION_BUDGET_CACHE_LIMIT:
                self._calls.popitem(last=False)
        return True

    @staticmethod
    def _error(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=(
                "Verification context limit reached. Decide using the evidence "
                "already gathered."
            ),
            name=request.tool_call.get("name", "unknown") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "unknown",
            tool_call_id=request.tool_call.get("id", "") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "",
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return handler(request)

        if (
            request.tool_call.get("name") not in self._tool_names
            or self._reserve(request)
        ):
            return handler(request)
        return self._error(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest], Awaitable[ToolMessage | Command[Any]]
        ],
    ) -> ToolMessage | Command[Any]:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return await handler(request)

        if (
            request.tool_call.get("name") not in self._tool_names
            or self._reserve(request)
        ):
            return await handler(request)
        return self._error(request)


class _RepositoryToolBudgetMiddleware(AgentMiddleware[Any, None]):
    """Bound repository inspection calls and read/result sizes."""

    def __init__(
        self,
        backend: BackendProtocol,
        *,
        root: str = "/",
        allowed_tools: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self._bounds = RepositoryBounds(
            backend, root=root, allowed_tools=allowed_tools
        )
        self._calls: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _operation_key(request: ToolCallRequest) -> str:
        for key in ("criteria_operation_id", "rubric_grading_operation_id"):
            operation_id = request.state.get(key)
            if isinstance(operation_id, str):
                return operation_id
        return "__legacy__"

    def _reserve_call(self, request: ToolCallRequest) -> bool:
        key = self._operation_key(request)
        with self._lock:
            count = self._calls.get(key, 0)
            if count >= _REPOSITORY_TOOL_CALL_LIMIT:
                return False
            self._calls[key] = count + 1
            self._calls.move_to_end(key)
            while len(self._calls) > _REPOSITORY_OPERATION_BUDGET_CACHE_LIMIT:
                self._calls.popitem(last=False)
        return True

    @staticmethod
    def _error(request: ToolCallRequest, message: str) -> ToolMessage:
        return ToolMessage(
            content=message,
            name=request.tool_call.get("name", "unknown") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "unknown",
            tool_call_id=request.tool_call.get("id", "") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "",
            status="error",
        )

    def _preflight(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else None
        if name is None:
            return None
        args = request.tool_call.get("args", {}) if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else {}
        error = self._bounds.preflight(name, args)
        return self._error(request, error) if error is not None else None

    async def _apreflight(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else None
        if name is None:
            return None
        args = request.tool_call.get("args", {}) if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else {}
        error = await self._bounds.apreflight(name, args)
        return self._error(request, error) if error is not None else None

    def _bound_result(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage:
        non_text = (
            "Non-text repository content omitted; criteria drafting supports "
            "text results only."
        )
        if not isinstance(result, ToolMessage) or not isinstance(
            result.content, str
        ):
            return self._error(request, non_text)
        bounded = self._bounds.bound_text(
            (request.tool_call.get("name") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "unknown") or "unknown", result.content
        )
        return result.model_copy(update={"content": bounded})

    def _bounded_request(self, request: ToolCallRequest) -> ToolCallRequest:
        name = request.tool_call.get("name") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else None
        if name is None:
            return request
        args = request.tool_call.get("args", {}) if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else {}
        new_tool_call = {**(request.tool_call if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else {}), "args": args}
        return request.override(tool_call=cast(ToolCall, new_tool_call))

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return handler(request)

        if request.tool_call.get("name") not in _REPOSITORY_TOOL_NAMES:
            return handler(request)

        if not self._reserve_call(request):
            return self._error(
                request,
                "Repository context limit reached. Draft the acceptance "
                "criteria now using the context already gathered.",
            )

        if error := self._preflight(request):
            return error

        request = self._bounded_request(request)
        return self._bound_result(request, handler(request))

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest], Awaitable[ToolMessage | Command[Any]]
        ],
    ) -> ToolMessage | Command[Any]:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return await handler(request)

        if request.tool_call.get("name") not in _REPOSITORY_TOOL_NAMES:
            return await handler(request)

        if not self._reserve_call(request):
            return self._error(
                request,
                "Repository context limit reached. Draft the acceptance "
                "criteria now using the context already gathered.",
            )

        if error := await self._apreflight(request):
            return error

        request = self._bounded_request(request)
        return self._bound_result(request, await handler(request))


class _WebSearchBudgetMiddleware(
    AgentMiddleware[GoalCriteriaAgentState, None]
):
    """Limit web searches independently for each nested context operation."""

    def __init__(self) -> None:
        super().__init__()
        self._calls: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def _reserve(self, request: ToolCallRequest) -> bool:
        key = _RepositoryToolBudgetMiddleware._operation_key(request)
        with self._lock:
            count = self._calls.get(key, 0)
            if count >= _WEB_SEARCH_CALL_LIMIT:
                return False
            self._calls[key] = count + 1
            self._calls.move_to_end(key)
            while len(self._calls) > _REPOSITORY_OPERATION_BUDGET_CACHE_LIMIT:
                self._calls.popitem(last=False)
        return True

    @staticmethod
    def _error(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=(
                "Web search limit reached. Continue using the available evidence "
                "and context already gathered."
            ),
            name=request.tool_call.get("name", "unknown") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "unknown",
            tool_call_id=request.tool_call.get("id", "") if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict) else "",
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return handler(request)

        if request.tool_call.get("name") != "web_search" or self._reserve(request):
            return handler(request)
        return self._error(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest], Awaitable[ToolMessage | Command[Any]]
        ],
    ) -> ToolMessage | Command[Any]:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return await handler(request)

        if request.tool_call.get("name") != "web_search" or self._reserve(request):
            return await handler(request)
        return self._error(request)


def _coerce_goal_proposal(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    objective = value.get("objective")
    criteria = value.get("criteria")
    if isinstance(objective, str) and isinstance(criteria, str):
        objective = objective.strip()
        criteria = criteria.strip()
        if objective and criteria:
            return objective, criteria
    structured = value.get("structured_response")
    if structured is not None:
        proposal = _coerce_goal_proposal(structured)
        if proposal is not None:
            return proposal
    for nested in value.values():
        if nested is structured:
            continue
        proposal = _coerce_goal_proposal(nested)
        if proposal is not None:
            return proposal
    return None


def _goal_proposal_from_text(text: str) -> tuple[str, str] | None:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    return _coerce_goal_proposal(value)


def _proposal_from_result(result: object) -> tuple[str, str] | None:
    proposal = _coerce_goal_proposal(result)
    if proposal is not None or not isinstance(result, dict):
        return proposal
    messages = result.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = str(message.content or "")
        elif isinstance(message, dict):
            content = message.get("content")
            if not isinstance(content, str):
                continue
            text = content
        else:
            continue
        proposal = _goal_proposal_from_text(text)
        if proposal is not None:
            return proposal
    return None


def _summarize_criteria_result(result: object) -> str:
    if isinstance(result, dict):
        keys = sorted(str(key) for key in result)
        messages = result.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            text: str | None = None
            if isinstance(last, AIMessage):
                text = str(last.content or "")
            elif isinstance(last, dict):
                content = last.get("content")
                text = content if isinstance(content, str) else None
            if text:
                text = text.strip()
                if len(text) > _CRITERIA_RESULT_LOG_LIMIT:
                    text = text[:_CRITERIA_RESULT_LOG_LIMIT] + "..."
                return f"keys={keys} last_message_text={text!r}"
        return f"keys={keys}"
    summary = repr(result)
    if len(summary) > _CRITERIA_RESULT_LOG_LIMIT:
        summary = summary[:_CRITERIA_RESULT_LOG_LIMIT] + "..."
    return summary


def _goal_criteria_request(value: object) -> GoalCriteriaRequest:
    if not isinstance(value, dict):
        msg = "Goal criteria request must be an object."
        raise TypeError(msg)
    request_id = value.get("request_id")
    kind = value.get("kind")
    objective = value.get("objective")
    if not isinstance(request_id, str) or not request_id.strip():
        msg = "Goal criteria request requires a request_id."
        raise ValueError(msg)
    if kind not in {"create", "amend"}:
        msg = "Goal criteria request kind must be create or amend."
        raise ValueError(msg)
    if not isinstance(objective, str) or not objective.strip():
        msg = "Goal criteria request requires an objective."
        raise ValueError(msg)

    optional: dict[str, str] = {}
    for key in ("criteria", "feedback", "previous_criteria"):
        item = value.get(key)
        if item is None:
            continue
        if not isinstance(item, str):
            msg = f"Goal criteria request field {key} must be text."
            raise TypeError(msg)
        optional[key] = item

    if kind == "amend":
        criteria = optional.get("criteria", "")
        feedback = optional.get("feedback", "")
        if not criteria.strip() or not feedback.strip():
            msg = "Goal amendment requests require criteria and feedback."
            raise ValueError(msg)
        return GoalAmendRequest(
            request_id=request_id,
            objective=objective,
            kind="amend",
            criteria=criteria,
            feedback=feedback,
        )

    create: GoalCreateRequest = {
        "request_id": request_id,
        "objective": objective,
        "kind": "create",
    }
    if "feedback" in optional:
        create["feedback"] = optional["feedback"]
    if "previous_criteria" in optional:
        create["previous_criteria"] = optional["previous_criteria"]
    return create


def _goal_criteria_prompt(request: GoalCriteriaRequest) -> str:
    if request["kind"] == "amend":
        return _goal_amendment_human_prompt(
            request["objective"],
            request["criteria"],
            request["feedback"],
        )
    return _goal_rubric_human_prompt(
        request["objective"],
        feedback=request.get("feedback"),
        previous_criteria=request.get("previous_criteria"),
    )


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {
            "text",
            "text-plain",
        }:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts).strip()


def _conversation_context(messages: Sequence[BaseMessage]) -> str:
    remaining = _CONVERSATION_CONTEXT_TOTAL_TEXT_LIMIT
    projected_reversed: list[BaseMessage] = []
    for message in reversed(messages):
        if is_conversation_control_message(message):
            continue
        if len(projected_reversed) >= _CONVERSATION_CONTEXT_MESSAGE_LIMIT:
            break
        if not isinstance(message, (HumanMessage, AIMessage)):
            continue
        text = _message_text(message)
        if not text:
            continue
        text = text[: min(_CONVERSATION_CONTEXT_MESSAGE_TEXT_LIMIT, remaining)]
        if not text:
            break
        projected_type = (
            HumanMessage if isinstance(message, HumanMessage) else AIMessage
        )
        projected_reversed.append(projected_type(content=text))
        remaining -= len(text)
        if remaining == 0:
            break

    projected = list(reversed(projected_reversed))
    while projected:
        serialized = get_buffer_string(projected, format="xml")
        if len(serialized) <= _CONVERSATION_CONTEXT_SERIALIZED_LIMIT:
            return serialized
        projected.pop(0)
    return ""


def _prompt_with_conversation_context(
    request: GoalCriteriaRequest,
    messages: Sequence[BaseMessage],
) -> str:
    prompt = _goal_criteria_prompt(request)
    context = _conversation_context(messages)
    if not context:
        return prompt
    return (
        f"{prompt}\n\n<conversation_context>\n"
        "The messages below are background context only. The explicit goal "
        "operation above is authoritative. Use this context to resolve what an "
        "underspecified objective refers to, then write criteria for that resolved "
        "work. Do not treat the context as a source of additional requirements: work "
        "it discusses that the objective does not ask for stays out of the criteria.\n"
        f"{context}\n"
        "</conversation_context>"
    )


@register_middleware(name="goal_criteria")
class GoalCriteriaMiddleware(AgentMiddleware[GoalCriteriaState, Any]):
    """Run goal-criteria requests entirely inside the main server graph."""

    state_schema = GoalCriteriaState

    def __init__(
        self,
        criteria_agent: Any | None = None,
        fallback_agent: Any | None = None,
    ) -> None:
        super().__init__()
        self._criteria_agent = criteria_agent
        self._fallback_agent = fallback_agent

    @staticmethod
    def _input(
        request: GoalCriteriaRequest,
        messages: Sequence[BaseMessage],
    ) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": _prompt_with_conversation_context(
                        request, messages
                    ),
                }
            ],
            "criteria_objective": request["objective"],
            "criteria_operation_id": request["request_id"],
        }

    @staticmethod
    def _update(
        request: GoalCriteriaRequest,
        result: object,
    ) -> dict[str, Any]:
        proposal = _proposal_from_result(result)
        if proposal is None:
            logger.warning(
                "Criteria agent returned no complete proposal; raw result: %s",
                _summarize_criteria_result(result),
            )
            msg = "The server criteria agent returned no complete proposal."
            raise RuntimeError(msg)
        proposed_objective, criteria = proposal
        objective = (
            request["objective"]
            if request["kind"] == "create"
            else proposed_objective
        )
        return {
            "goal_criteria_request": None,
            "rubric": None,
            "_pending_goal_objective": objective,
            "_pending_goal_rubric": criteria,
            "_pending_goal_kind": request["kind"],
            "_pending_goal_request_id": request["request_id"],
            "jump_to": "end",
        }

    @hook_config(can_jump_to=["end"])
    def before_agent(
        self,
        state: GoalCriteriaState,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        value = state.get("goal_criteria_request")
        if value is None or self._criteria_agent is None:
            return None
        request = _goal_criteria_request(value)
        child_input = self._input(request, state.get("messages", []))
        try:
            result = self._criteria_agent.invoke(
                child_input, context=runtime.context
            )
        except _CRITERIA_FALLBACK_ERRORS:
            if self._fallback_agent is None:
                raise
            logger.warning(
                "Criteria context agent failed; drafting from the goal alone",
                exc_info=True,
            )
            result = self._fallback_agent.invoke(
                child_input, context=runtime.context
            )
        else:
            if (
                self._fallback_agent is not None
                and _proposal_from_result(result) is None
            ):
                logger.warning(
                    "Criteria context agent returned no proposal; drafting from "
                    "the goal alone",
                )
                result = self._fallback_agent.invoke(
                    child_input, context=runtime.context
                )
        return self._update(request, result)

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self,
        state: GoalCriteriaState,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        value = state.get("goal_criteria_request")
        if value is None or self._criteria_agent is None:
            return None
        request = _goal_criteria_request(value)
        child_input = self._input(request, state.get("messages", []))
        try:
            result = await self._criteria_agent.ainvoke(
                child_input, context=runtime.context
            )
        except _CRITERIA_FALLBACK_ERRORS:
            if self._fallback_agent is None:
                raise
            logger.warning(
                "Criteria context agent failed; drafting from the goal alone",
                exc_info=True,
            )
            result = await self._fallback_agent.ainvoke(
                child_input, context=runtime.context
            )
        else:
            if (
                self._fallback_agent is not None
                and _proposal_from_result(result) is None
            ):
                logger.warning(
                    "Criteria context agent returned no proposal; drafting from "
                    "the goal alone",
                )
                result = await self._fallback_agent.ainvoke(
                    child_input, context=runtime.context
                )
        return self._update(request, result)


def create_goal_criteria_agent(
    *,
    model: str | BaseChatModel,
    repository_backend: BackendProtocol | None,
    repository_root: str = "/",
    context_tools: Sequence[BaseTool | Callable[..., Any]] = (),
    subagent_metas: Sequence[SubagentMetadata] | None = None,
    skill_sources: Sequence[tuple[str, ...]] | None = None,
) -> Any:
    """Create the ephemeral server-side criteria agent graph."""
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware import FilesystemMiddleware
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from langchain_core.tools import BaseTool as _BaseTool, StructuredTool

    from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware
    from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
    from k8s_autopilot.middleware.subagents import SubagentsMiddleware
    from k8s_autopilot.middleware.unified_system_message import (
        UnifiedSystemMessageMiddleware,
    )

    normalized_context_tools: list[_BaseTool] = []
    for t in context_tools:
        if isinstance(t, _BaseTool):
            normalized_context_tools.append(t)
        elif inspect.iscoroutinefunction(t):
            normalized_context_tools.append(
                StructuredTool.from_function(coroutine=t)
            )
        else:
            normalized_context_tools.append(StructuredTool.from_function(func=t))

    reserved_names = {_STRUCTURED_OUTPUT_TOOL_NAME}
    if repository_backend is not None:
        reserved_names.update(_REPOSITORY_TOOL_NAMES)
    conflicting_names = sorted(
        t.name for t in normalized_context_tools if t.name in reserved_names
    )
    if conflicting_names:
        names = ", ".join(conflicting_names)
        msg = f"Context tool names conflict with criteria-agent tools: {names}."
        raise ValueError(msg)

    middleware: list[AgentMiddleware[Any, Any]] = [
        ConfigurableModelMiddleware(persist_model_state=False),
        _GoalContextFallbackMiddleware(),
        _WebSearchBudgetMiddleware(),
        _CriteriaContextBudgetMiddleware(),
    ]
    if repository_backend is not None:
        fs_tools = ["read_file", "ls", "execute"]
        fs_kwargs: dict[str, Any] = {
            "backend": repository_backend,
            "tools": fs_tools,
            "tool_token_limit_before_evict": None,
        }
        middleware.extend(
            [
                FilesystemMiddleware(**fs_kwargs),
                _RepositoryToolBudgetMiddleware(
                    repository_backend,
                    root=repository_root,
                    allowed_tools=fs_tools,
                ),
            ]
        )

    # Attach skills middleware: encompasses agent, plugin, and subagent skills in planning mode
    skills_backend = repository_backend or FilesystemBackend(virtual_mode=False)
    if skill_sources is not None:
        middleware.append(
            PluginSkillsMiddleware(
                backend=skills_backend,
                sources=skill_sources,
                planning_mode=True,
            )
        )
    else:
        middleware.append(
            PluginSkillsMiddleware(
                backend=skills_backend,
                include_subagent_skills=True,
                subagents=subagent_metas,
                planning_mode=True,
            )
        )

    # Attach subagents middleware: provides capability overview without js_eval or execution instructions
    effective_subagent_metas = (
        list(subagent_metas) if subagent_metas is not None else []
    )
    if not effective_subagent_metas:
        try:
            from k8s_autopilot.subagents.loader import (
                get_built_in_subagents,
                list_subagents,
            )

            effective_subagent_metas.extend(get_built_in_subagents())
            effective_subagent_metas.extend(list_subagents())
        except Exception as exc:
            logger.debug(
                "Could not discover default subagents for criteria agent: %s",
                exc,
            )

    if effective_subagent_metas:
        middleware.append(
            SubagentsMiddleware(
                subagent_metas=effective_subagent_metas,
                planning_mode=True,
            )
        )

    from k8s_autopilot.middleware.auto_mode import AsyncApprovalHITLMiddleware

    criteria_interrupt_on: dict[str, Any] = {
        t.name: True
        for t in normalized_context_tools
    }
    if criteria_interrupt_on:
        middleware.append(AsyncApprovalHITLMiddleware(criteria_interrupt_on))

    # Collapse all system message blocks into a single consolidated string
    middleware.append(UnifiedSystemMessageMiddleware())

    return create_agent(
        model=model,
        tools=normalized_context_tools,
        middleware=middleware,
        system_prompt=GOAL_RUBRIC_SYSTEM_PROMPT,
        response_format=ToolStrategy(schema=GoalProposal),
        state_schema=GoalCriteriaAgentState,
        name="goal_criteria_agent",
    ).with_config(
        {
            "recursion_limit": _REPOSITORY_RECURSION_LIMIT,
            "run_name": "K8s Autopilot goal criteria generation",
        }
    )


def create_goal_criteria_fallback_agent(
    *,
    model: str | BaseChatModel,
) -> Any:
    """Create the goal-only fallback agent for criteria generation."""
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy

    from k8s_autopilot.middleware.configurable_model import ConfigurableModelMiddleware
    from k8s_autopilot.middleware.unified_system_message import (
        UnifiedSystemMessageMiddleware,
    )

    middleware: list[AgentMiddleware[Any, Any]] = [
        ConfigurableModelMiddleware(persist_model_state=False),
        UnifiedSystemMessageMiddleware(),
    ]
    return create_agent(
        model=model,
        tools=[],
        middleware=middleware,
        system_prompt=GOAL_RUBRIC_SYSTEM_PROMPT,
        response_format=ToolStrategy(schema=GoalProposal),
        state_schema=GoalCriteriaAgentState,
        name="goal_criteria_fallback_agent",
    ).with_config(
        {
            "recursion_limit": _FALLBACK_RECURSION_LIMIT,
            "run_name": "K8s Autopilot goal criteria fallback",
        }
    )


__all__ = [
    "GoalAmendRequest",
    "GoalCreateRequest",
    "GoalCriteriaAgentState",
    "GoalCriteriaMiddleware",
    "GoalCriteriaRequest",
    "GoalCriteriaState",
    "GoalProposal",
    "_CriteriaContextBudgetMiddleware",
    "_GoalContextFallbackMiddleware",
    "_RepositoryToolBudgetMiddleware",
    "_WebSearchBudgetMiddleware",
    "create_goal_criteria_agent",
    "create_goal_criteria_fallback_agent",
]
