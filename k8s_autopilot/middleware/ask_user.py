"""Ask User middleware for interactive question-answering during agent execution.

Provides the ``ask_user`` tool that allows the agent to ask the user one or
more questions when clarification or input is needed. Uses LangGraph's
``interrupt()`` mechanism to pause execution and wait for user responses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import contextlib
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import InjectedToolCallId
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt

from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.middleware.unified_system_message import unify_system_message
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class Choice(TypedDict):
    """Single selectable option in an ask_user question."""

    value: str


class Question(TypedDict):
    """Structured question with choices for human-in-the-loop prompts."""

    question: str
    type: Literal["text", "multiple_choice"]
    choices: NotRequired[list[Choice]]
    required: NotRequired[bool]


class AskUserRequest(TypedDict):
    """Complete ask_user interrupt payload with one or more questions."""

    type: Literal["ask_user"]
    questions: list[Question]
    tool_call_id: str


ASK_USER_TOOL_DESCRIPTION = """Ask the user one or more questions when you need clarification or input before proceeding.

Each question can be either:
- "text": Free-form text response from the user
- "multiple_choice": User selects from predefined options (an "Other" option is always available)

For multiple choice questions, provide a list of choices. The user can pick one or type a custom answer via the "Other" option.

By default all questions are required. Set "required" to false for optional questions that the user can skip. Do not include "(required)", "(optional)", "- optional", or similar annotations in the question text — the UI renders that separately based on the "required" field.

Use this tool when:
- You need clarification on ambiguous requirements
- You want the user to choose between multiple valid approaches
- You need specific information only the user can provide
- You want to confirm a plan before executing it

Do NOT use this tool for:
- Simple yes/no confirmations (just proceed with your best judgment)
- Questions you can answer yourself from context
- Trivial decisions that don't meaningfully affect the outcome"""

ASK_USER_SYSTEM_PROMPT = """## `ask_user`

You have access to the `ask_user` tool to ask the user questions when you need clarification or input.
Use this tool sparingly - only when you genuinely need information from the user that you cannot determine from context.

When using `ask_user`:
- Be concise and specific with your questions
- Use multiple choice when there are clear options to choose from
- Use text input when you need free-form responses
- Group related questions into a single ask_user call rather than making multiple calls
- Never ask questions you can answer yourself from the available context"""


def _validate_questions(questions: list[Question]) -> None:
    if not questions:
        raise ValueError("ask_user requires at least one question")

    for q in questions:
        question_text = q.get("question")
        if not isinstance(question_text, str) or not question_text.strip():
            raise ValueError("ask_user questions must have non-empty 'question' text")

        question_type = q.get("type")
        if question_type not in {"text", "multiple_choice"}:
            raise ValueError(f"unsupported ask_user question type: {question_type!r}")

        if question_type == "multiple_choice" and not q.get("choices"):
            raise ValueError(f"multiple_choice question {q.get('question')!r} requires non-empty 'choices'")

        if question_type == "text" and q.get("choices"):
            raise ValueError(f"text question {q.get('question')!r} must not define 'choices'")


def _parse_answers(
    response: object,
    questions: list[Question],
    tool_call_id: str,
) -> Command[Any]:
    import json

    status: str = "answered"
    error_text: str | None = None
    answers: list[str] = []

    # If response is a string, check if it's JSON encoded
    if isinstance(response, str):
        trimmed = response.strip()
        if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
            with contextlib.suppress(Exception):
                response = json.loads(trimmed)

    if isinstance(response, str):
        answers = [response]
        status = "answered"
    elif isinstance(response, list):
        answers = [str(x) for x in response]
        status = "answered"
    elif isinstance(response, dict):
        response_dict = cast("dict[str, Any]", response)
        response_status = response_dict.get("status")
        if isinstance(response_status, str):
            status = response_status

        if "answers" not in response_dict:
            if "choice" in response_dict:
                answers = [str(response_dict["choice"])]
            elif "text" in response_dict:
                answers = [str(response_dict["text"])]
            elif status == "answered":
                answers = []
                status = "error"
                error_text = "missing ask_user answers payload"
            else:
                answers = []
        else:
            raw_answers = response_dict["answers"]
            if isinstance(raw_answers, list):
                answers = [str(answer) for answer in raw_answers]
            elif isinstance(raw_answers, str):
                answers = [raw_answers]
            else:
                answers = []
                status = "error"
                error_text = "invalid ask_user answers payload"

        if status == "error":
            response_error = response_dict.get("error")
            if isinstance(response_error, str) and response_error:
                error_text = response_error
        elif status == "cancelled":
            answers = ["(cancelled)" for _ in questions]
    else:
        answers = []
        status = "error"
        error_text = "invalid ask_user response payload"

    if status == "error":
        detail = error_text or "ask_user interaction failed"
        answers = [f"(error: {detail})" for _ in questions]

    formatted_answers = []
    for i, q in enumerate(questions):
        raw_ans = answers[i] if i < len(answers) else ""
        ans_str = raw_ans.strip()
        if not ans_str:
            ans_str = "(skipped / no additional details)" if q.get("required") is False else "(no answer provided)"
        formatted_answers.append(f"Q: {q['question']}\nA: {ans_str}")
    result_text = "\n\n".join(formatted_answers)

    logger.info(
        "Processed ask_user answers for tool message",
        extra={
            "tool_call_id": tool_call_id,
            "status": status,
            "answers": answers,
            "result_preview": result_text[:200],
        },
    )

    return Command(
        update={
            "messages": [ToolMessage(result_text, tool_call_id=tool_call_id, name="ask_user")],
        }
    )


@register_middleware(name="ask_user")
class AskUserMiddleware(AgentMiddleware[Any, Any]):
    """Expose ask_user tool and inject system guidance into model requests."""

    def __init__(
        self,
        *,
        system_prompt: str = ASK_USER_SYSTEM_PROMPT,
        tool_description: str = ASK_USER_TOOL_DESCRIPTION,
    ) -> None:
        """Initialize AskUserMiddleware and register the ask_user tool.

        Args:
            system_prompt: Guidance prompt injected into the model's system message.
            tool_description: Custom description for the ask_user tool.
        """
        super().__init__()
        self.system_prompt = system_prompt
        self.tool_description = tool_description

        @tool(description=self.tool_description)
        def _ask_user(
            questions: list[Question],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command[Any]:
            _validate_questions(questions)
            ask_request = AskUserRequest(
                type="ask_user",
                questions=questions,
                tool_call_id=tool_call_id,
            )
            response = interrupt(ask_request)
            return _parse_answers(response, questions, tool_call_id)

        _ask_user.name = "ask_user"
        self.tools = [_ask_user]

    def _with_ask_user_prompt(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        prompt = self.system_prompt
        system_msg = request.system_message
        if system_msg:
            unified = unify_system_message(system_msg)
            content_str = str(unified.content) if unified and unified.content else ""
            if prompt not in content_str:
                new_msg = append_to_system_message(system_msg, prompt)
                return request.override(system_message=new_msg)
            return request
        return request.override(system_message=SystemMessage(content=prompt))

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Wrap synchronous model call to inject ask_user system instructions.

        Args:
            request: Model invocation request.
            handler: Next synchronous model handler.

        Returns:
            ModelResponse from the handler.
        """
        return handler(self._with_ask_user_prompt(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Wrap asynchronous model call to inject ask_user system instructions.

        Args:
            request: Model invocation request.
            handler: Next asynchronous model handler.

        Returns:
            ModelResponse from the handler.
        """
        return await handler(self._with_ask_user_prompt(request))
