"""Ask user middleware for interactive question-answering during agent execution."""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast
from pydantic import Field
from typing_extensions import NotRequired, TypedDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import InjectedToolCallId
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt

logger = AgentLogger("AskUserMW")


# ---------------------------------------------------------------------------
# Inline Types from _ask_user_types.py
# ---------------------------------------------------------------------------

class Choice(TypedDict):
    value: Annotated[str, Field(description="The display label for this choice.")]


class Question(TypedDict):
    question: Annotated[str, Field(description="The question text to display.")]
    type: Annotated[
        Literal["text", "multiple_choice"],
        Field(
            description=(
                "Question type. 'text' for free-form input, 'multiple_choice' for "
                "predefined options."
            )
        ),
    ]
    choices: NotRequired[
        Annotated[
            list[Choice],
            Field(
                description=(
                    "Options for multiple_choice questions. An 'Other' free-form "
                    "option is always appended automatically."
                )
            ),
        ]
    ]
    required: NotRequired[
        Annotated[
            bool,
            Field(
                description="Whether the user must answer. Defaults to true if omitted."
            ),
        ]
    ]


class AskUserRequest(TypedDict):
    type: Literal["ask_user"]
    questions: list[Question]
    tool_call_id: str


class AskUserAnswered(TypedDict):
    type: Literal["answered"]
    answers: list[str]


class AskUserCancelled(TypedDict):
    type: Literal["cancelled"]


AskUserWidgetResult = AskUserAnswered | AskUserCancelled


# ---------------------------------------------------------------------------
# Middleware Implementation
# ---------------------------------------------------------------------------

ASK_USER_TOOL_DESCRIPTION = """Ask the user one or more questions when you need clarification or input before proceeding."""

ASK_USER_SYSTEM_PROMPT = """## `ask_user`

You have access to the `ask_user` tool to ask the user questions when you need clarification or input.
Use this tool sparingly - only when you genuinely need information from the user that you cannot determine from context."""


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
            raise ValueError(f"multiple_choice question {question_text!r} requires choices")

        if question_type == "text" and q.get("choices"):
            raise ValueError(f"text question {question_text!r} must not define 'choices'")


def _parse_answers(
    response: object,
    questions: list[Question],
    tool_call_id: str,
) -> Command[Any]:
    status: str = "answered"
    error_text: str | None = None
    answers: list[str] = []

    if not isinstance(response, dict):
        status = "error"
        error_text = "invalid ask_user response payload"
    else:
        response_dict = cast("dict[str, Any]", response)
        response_status = response_dict.get("status")
        if isinstance(response_status, str):
            status = response_status

        if "answers" not in response_dict:
            if status == "answered":
                status = "error"
                error_text = "missing ask_user answers payload"
        else:
            raw_answers = response_dict["answers"]
            if isinstance(raw_answers, list):
                answers = [str(answer) for answer in raw_answers]
            else:
                status = "error"
                error_text = "invalid ask_user answers payload"

        if status == "error":
            response_error = response_dict.get("error")
            if isinstance(response_error, str) and response_error:
                error_text = response_error
        elif status == "cancelled":
            answers = ["(cancelled)" for _ in questions]
        elif status == "answered":
            if len(answers) != len(questions):
                logger.warning(f"ask_user answer count mismatch: expected {len(questions):d}, got {len(answers):d}")

    if status == "error":
        detail = error_text or "ask_user interaction failed"
        answers = [f"(error: {detail})" for _ in questions]

    formatted_answers = []
    for i, q in enumerate(questions):
        answer = answers[i] if i < len(answers) else "(no answer)"
        formatted_answers.append(f"Q: {q['question']}\nA: {answer}")
    result_text = "\n\n".join(formatted_answers)
    return Command(
        update={
            "messages": [ToolMessage(result_text, tool_call_id=tool_call_id)],
        }
    )


from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware


@register_middleware(name="ask_user")
class AskUserMiddleware(BaseAgentMiddleware):
    """Middleware that provides an ask_user tool for interactive questioning."""

    def __init__(
        self,
        *,
        system_prompt: str = ASK_USER_SYSTEM_PROMPT,
        tool_description: str = ASK_USER_TOOL_DESCRIPTION,
    ) -> None:
        super().__init__()
        self.system_prompt = system_prompt
        self.tool_description = tool_description

        @tool(description=self.tool_description)
        def _ask_user(
            questions: list[Question],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command[Any]:
            """Ask the user one or more questions."""
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

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        if request.system_message is not None:
            new_system_content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{self.system_prompt}"},
            ]
        else:
            new_system_content = [{"type": "text", "text": self.system_prompt}]
        new_system_message = SystemMessage(
            content=cast("list[str | dict[str, str]]", new_system_content)
        )
        return handler(request.override(system_message=new_system_message))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT] | AIMessage:
        if request.system_message is not None:
            new_system_content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{self.system_prompt}"},
            ]
        else:
            new_system_content = [{"type": "text", "text": self.system_prompt}]
        new_system_message = SystemMessage(
            content=cast("list[str | dict[str, str]]", new_system_content)
        )
        return await handler(request.override(system_message=new_system_message))
