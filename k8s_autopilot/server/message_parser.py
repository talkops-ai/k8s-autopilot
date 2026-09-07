"""Universal message parsing for K8s Autopilot.

Handles all message types from all model providers in a backend-agnostic way.
Works for:
- Live stream events from Pregel graph
- Stored messages from checkpoint DB (SQLite/Postgres)
- A2A protocol responses and client rendering
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)

from k8s_autopilot.integrations.stream_bridge import _extract_text_and_thinking
from k8s_autopilot.middleware.goal_state_notice import is_conversation_control_message

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class MessageRole(str, Enum):
    """Universal message role."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class ToolCallStatus(str, Enum):
    """Tool call lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class ParsedToolCall:
    """Parsed tool call from an AI message or tool execution."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.PENDING
    result: str | None = None


@dataclass
class ParsedMessage:
    """Universal parsed message format.

    The canonical representation consumed by A2A, A2UI, and thread history.
    """

    role: MessageRole
    content: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    thinking: str = ""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    is_control: bool = False


class MessageParser:
    """Parser that converts various message formats to canonical ParsedMessage."""

    @classmethod
    def parse_message(cls, msg: Any) -> ParsedMessage | None:
        """Parse a single message object or dict into a ParsedMessage."""
        if msg is None:
            return None

        # Check if conversation control message (e.g. goal notice)
        is_control = is_conversation_control_message(msg)

        # 1. LangChain HumanMessage
        if isinstance(msg, HumanMessage):
            content = str(msg.content)
            return ParsedMessage(
                id=getattr(msg, "id", None) or str(uuid.uuid4()),
                role=MessageRole.USER,
                content=content,
                is_control=is_control,
                metadata=getattr(msg, "additional_kwargs", {}),
            )

        # 2. LangChain AIMessage
        if isinstance(msg, AIMessage):
            text, thinking = _extract_text_and_thinking(
                msg.content,
                additional_kwargs=getattr(msg, "additional_kwargs", None),
                response_metadata=getattr(msg, "response_metadata", None),
                msg_obj=msg,
            )
            tool_calls = []
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(
                        ParsedToolCall(
                            id=tc.get("id") or str(uuid.uuid4()),
                            name=tc.get("name") or "tool",
                            args=tc.get("args") or {},
                            status=ToolCallStatus.PENDING,
                        )
                    )
            return ParsedMessage(
                id=getattr(msg, "id", None) or str(uuid.uuid4()),
                role=MessageRole.ASSISTANT,
                content=text,
                thinking=thinking,
                tool_calls=tool_calls,
                is_control=is_control,
                metadata=getattr(msg, "response_metadata", {}),
            )

        # 3. LangChain ToolMessage
        if isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", "") or ""
            name = getattr(msg, "name", "tool") or "tool"
            content = str(msg.content)
            status = (
                ToolCallStatus.ERROR
                if getattr(msg, "status", None) == "error"
                else ToolCallStatus.SUCCESS
            )
            tc = ParsedToolCall(
                id=call_id,
                name=name,
                status=status,
                result=content,
            )
            return ParsedMessage(
                id=getattr(msg, "id", None) or str(uuid.uuid4()),
                role=MessageRole.TOOL,
                content=content,
                tool_calls=[tc],
                is_control=is_control,
            )

        # 4. LangChain SystemMessage
        if isinstance(msg, SystemMessage):
            return ParsedMessage(
                id=getattr(msg, "id", None) or str(uuid.uuid4()),
                role=MessageRole.SYSTEM,
                content=str(msg.content),
                is_control=is_control,
            )

        # 5. Raw Dictionary format
        if isinstance(msg, dict):
            role_str = msg.get("role") or msg.get("type", "user")
            try:
                role = MessageRole(role_str)
            except ValueError:
                role = MessageRole.USER
            content = str(msg.get("content", ""))
            thinking = str(msg.get("thinking", ""))
            return ParsedMessage(
                id=msg.get("id") or str(uuid.uuid4()),
                role=role,
                content=content,
                thinking=thinking,
                metadata=msg.get("metadata", {}),
                is_control=is_control,
            )

        return None

    @classmethod
    def parse_messages(
        cls,
        messages: Sequence[Any],
        *,
        include_control: bool = False,
    ) -> list[ParsedMessage]:
        """Parse a sequence of messages, optionally filtering control messages."""
        parsed: list[ParsedMessage] = []
        for msg in messages:
            p = cls.parse_message(msg)
            if p is not None:
                if not include_control and p.is_control:
                    continue
                parsed.append(p)

        # Correlate tool calls with results
        tool_results: dict[str, ParsedToolCall] = {}
        for p in parsed:
            if p.role == MessageRole.TOOL:
                for tc in p.tool_calls:
                    if tc.id:
                        tool_results[tc.id] = tc

        for p in parsed:
            if p.role == MessageRole.ASSISTANT:
                for tc in p.tool_calls:
                    if tc.id in tool_results:
                        res = tool_results[tc.id]
                        tc.status = res.status
                        tc.result = res.result

        return parsed
