"""Middleware to collapse list-based system message content into a single string."""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
import re
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AnyMessage, SystemMessage

from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_stringified_content_blocks(content: str) -> str | None:
    r"""Parse stringified Python lists of dicts e.g. \"[{'type': 'text', ...}]\"."""
    trimmed = content.strip()
    if not (trimmed.startswith(("[{'type':", '[{"type":')) and "}]" in trimmed):
        return None

    split_idx = trimmed.find("}]") + 2
    list_part = trimmed[:split_idx]
    rest_part = trimmed[split_idx:].strip()

    try:
        parsed = ast.literal_eval(list_part)
        if isinstance(parsed, list):
            unpacked_parts: list[str] = []
            for item in parsed:
                if isinstance(item, dict):
                    t = str(item.get("text") or item.get("content") or "").strip()
                    if t:
                        unpacked_parts.append(t)
                elif isinstance(item, str) and item.strip():
                    unpacked_parts.append(item.strip())
            if rest_part:
                unpacked_parts.append(rest_part)
            return "\n\n".join(unpacked_parts)
    except Exception:
        logger.debug("ast.literal_eval failed on stringified blocks; trying regex")

    try:
        pattern = re.compile(
            r"['\"](?:text|content)['\"]\s*:\s*(['\"])(.*?)(?<!\\)\1",
            re.DOTALL,
        )
        matches = pattern.findall(list_part)
        if matches:
            regex_parts: list[str] = []
            for _, match_text in matches:
                cleaned = match_text.encode("utf-8").decode("unicode_escape", errors="replace").strip()
                if cleaned:
                    regex_parts.append(cleaned)
            if rest_part:
                regex_parts.append(rest_part)
            return "\n\n".join(regex_parts)
    except Exception:
        logger.debug("Regex unescape failed on stringified blocks")

    return None


def unify_system_message(system_message: SystemMessage | None) -> SystemMessage | None:
    """Normalize SystemMessage content from list of blocks or stringified list to a single string."""
    if system_message is None:
        return None

    content = getattr(system_message, "content", None)
    if isinstance(content, str):
        parsed_str = _parse_stringified_content_blocks(content)
        if parsed_str is not None:
            return SystemMessage(content=parsed_str)
        return system_message

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                txt = str(block.get("text") or block.get("content") or "")
                if txt:
                    parts.append(txt)
            elif isinstance(block, str) and block:
                parts.append(block)
        unified_text = "".join(parts).strip()
        return SystemMessage(content=unified_text)

    return system_message


@register_middleware(name="unified_system_message")
class UnifiedSystemMessageMiddleware(AgentMiddleware[Any, Any]):
    """Middleware that collapses SystemMessage content blocks into a single string."""

    def _normalize_request(self, request: ModelRequest) -> ModelRequest:
        overrides: dict[str, Any] = {}
        unified_msg = unify_system_message(request.system_message)
        if unified_msg is not None and unified_msg is not request.system_message:
            overrides["system_message"] = unified_msg

        if getattr(request, "messages", None):
            new_messages: list[AnyMessage] = []
            modified_msgs = False
            for msg in request.messages:
                if isinstance(msg, SystemMessage):
                    unif = unify_system_message(msg)
                    if unif is not None and unif is not msg:
                        new_messages.append(unif)
                        modified_msgs = True
                        continue
                new_messages.append(msg)
            if modified_msgs:
                overrides["messages"] = new_messages

        if overrides:
            return request.override(**overrides)
        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Wrap synchronous model call to normalize system messages into the expected schema.

        Args:
            request: Model request to normalize.
            handler: Synchronous model handler.

        Returns:
            Model response from the handler.
        """
        return handler(self._normalize_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Wrap asynchronous model call to normalize system messages into the expected schema.

        Args:
            request: Model request to normalize.
            handler: Asynchronous model handler.

        Returns:
            Model response from the handler.
        """
        return await handler(self._normalize_request(request))
