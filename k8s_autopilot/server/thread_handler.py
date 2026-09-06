"""A2A protocol handler for thread management and conversation history.

Provides thread lifecycle operations accessible via A2A protocol:
- List threads (with search/filter)
- Resume thread (switch to previous conversation)
- Delete thread
- Create new thread
- Get thread history (render previous messages)

Ported from opscode session management patterns.
"""

from __future__ import annotations

import logging
from typing import Any

from k8s_autopilot.server.message_parser import MessageParser, ParsedMessage
from k8s_autopilot.state.session import (
    ThreadInfo,
    delete_thread,
    format_relative_timestamp,
    generate_thread_id,
    list_threads,
)

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class A2AThreadHandler:
    """Handles thread management requests from A2A / A2UI clients."""

    def __init__(self, current_thread_id: str | None = None) -> None:
        self._current_thread_id = current_thread_id

    @property
    def current_thread_id(self) -> str | None:
        return self._current_thread_id

    def set_current_thread(self, thread_id: str) -> None:
        self._current_thread_id = thread_id

    async def list_threads(
        self,
        *,
        limit: int = 20,
        search_query: str | None = None,
    ) -> list[dict[str, Any]]:
        """List conversation threads with metadata.

        Returns:
            List of thread summaries formatted for A2A clients.
        """
        threads: list[ThreadInfo] = await list_threads(limit=limit)

        result: list[dict[str, Any]] = []
        for t in threads:
            tid = t.get("thread_id", "")
            prompt = t.get("initial_prompt") or ""

            if search_query:
                q = search_query.lower()
                if q not in tid.lower() and q not in prompt.lower():
                    continue

            result.append(
                {
                    "thread_id": tid,
                    "initial_prompt": prompt,
                    "message_count": t.get("message_count", 0),
                    "updated_at": format_relative_timestamp(t.get("updated_at")),
                    "created_at": format_relative_timestamp(t.get("created_at")),
                    "is_active": (tid == self._current_thread_id),
                }
            )

        return result

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete a thread from the session store."""
        success = await delete_thread(thread_id)
        if success and self._current_thread_id == thread_id:
            self._current_thread_id = None
        return success

    def create_thread(self) -> str:
        """Generate a new UUID7 thread ID."""
        new_id = generate_thread_id()
        self._current_thread_id = new_id
        return new_id

    async def get_thread_history(
        self,
        thread_id: str,
        checkpointer: Any = None,
    ) -> list[dict[str, Any]]:
        """Fetch and parse message history for a thread from the checkpointer.

        Returns:
            List of serialized ParsedMessage dictionaries for A2A clients.
        """
        if checkpointer is None:
            return []

        config = {"configurable": {"thread_id": thread_id}}
        try:
            state = await checkpointer.aget(config)
            if not state or not hasattr(state, "values"):
                return []

            raw_messages = state.values.get("messages", [])
            parsed = MessageParser.parse_messages(raw_messages, include_control=False)
            return [
                {
                    "id": p.id,
                    "role": p.role.value,
                    "content": p.content,
                    "thinking": p.thinking,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "args": tc.args,
                            "status": tc.status.value,
                            "result": tc.result,
                        }
                        for tc in p.tool_calls
                    ],
                    "timestamp": p.timestamp,
                    "metadata": p.metadata,
                }
                for p in parsed
            ]
        except Exception as exc:
            logger.warning("Failed to fetch thread history for %s: %s", thread_id, exc)
            return []
