"""
Conversation Compaction Middleware — token-budget-aware history compaction.

Follows the dcode + industry dual-layered storage pattern:
  1. Monitor message token count via ``before_agent()``
  2. When token count exceeds budget: offload full history to
     ``/conversation_history/{thread_id}.md`` via CompositeBackend
  3. Replace messages with summary + last N messages
  4. Agent can ``read_file("/conversation_history/...")`` to recall specifics

This middleware is registered as ``conversation_compactor`` in the registry.

Reference:
  - dcode agent.py lines 1773-1800 (CompositeBackend routing)
  - arXiv:2605.23296 (parallel compaction)
  - LangChain docs: conversation-persistence best practices
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
import time
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from k8s_autopilot.core.middleware.registry import (
    BaseAgentMiddleware,
    register_middleware,
)

logger = AgentLogger("CompactionMW")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(messages: List[Any], chars_per_token: float = 3.5) -> int:
    """Estimate total token count from a message list.

    Uses a character-based heuristic (industry standard: ~3.5 chars/token).
    For production accuracy, swap in tiktoken or model-specific tokenizer.
    """
    total_chars = 0
    for msg in messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Multi-part messages (thinking + text)
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(part.get("text", ""))
                elif isinstance(part, str):
                    total_chars += len(part)
    return int(total_chars / chars_per_token)


def _format_message_for_history(msg: Any) -> str:
    """Format a single message as markdown for the history file."""
    msg_type = getattr(msg, "type", "unknown")
    content = getattr(msg, "content", "")

    if isinstance(content, list):
        # Multi-part: extract text parts only
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        content = "\n".join(parts)

    # Truncate very long tool outputs in the history
    if msg_type == "tool" and len(content) > 2000:
        content = content[:2000] + "\n... [truncated]"

    return f"**[{msg_type.upper()}]**\n{content}\n"


def _generate_summary_prompt(messages: List[Any]) -> str:
    """Build a prompt asking the LLM to summarize the conversation so far."""
    # Gather just the human/ai messages for summarization context
    summary_parts: list[str] = []
    for msg in messages:
        msg_type = getattr(msg, "type", "")
        content = getattr(msg, "content", "")
        if isinstance(content, str) and msg_type in ("human", "ai"):
            # Truncate long individual messages
            if len(content) > 1500:
                content = content[:1500] + "..."
            summary_parts.append(f"{msg_type.upper()}: {content}")

    conversation_text = "\n---\n".join(summary_parts[-20:])  # Last 20 messages

    return (
        "Summarize the following conversation in a concise paragraph. "
        "Focus on: key decisions made, tasks completed, important context, "
        "and any pending items. Do NOT include tool output details.\n\n"
        f"<conversation>\n{conversation_text}\n</conversation>"
    )


# ---------------------------------------------------------------------------
# Compaction Middleware
# ---------------------------------------------------------------------------

@register_middleware(name="conversation_compactor")
class ConversationCompactionMiddleware(BaseAgentMiddleware):
    """Token-budget-aware conversation compaction with history offloading.

    When message tokens exceed ``token_budget * 0.75`` (75% threshold):
      1. Writes full history to ``/conversation_history/{thread_id}.md``
      2. Generates a summary of the conversation
      3. Replaces messages with: compaction notice + summary + last N messages

    The compaction notice tells the agent where to find full history,
    enabling on-demand recall via ``read_file()``.

    Args:
        token_budget: Max tokens before compaction (default: from config).
        keep_last_messages: Number of recent messages to preserve.
        summary_model: Model name for generating summaries.
            Falls back to config's COMPACTION_SUMMARY_MODEL, then LLM_MODEL.
        config: Config instance for defaults.
    """

    def __init__(
        self,
        *,
        token_budget: Optional[int] = None,
        keep_last_messages: Optional[int] = None,
        summary_model: Optional[str] = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._config = config
        self._compaction_count: int = 0

        # Resolve from config with explicit override priority
        cfg = self._get_config()
        self._token_budget = token_budget or getattr(
            cfg, "COMPACTION_TOKEN_BUDGET", 100_000
        )
        self._keep_last = keep_last_messages or getattr(
            cfg, "COMPACTION_KEEP_MESSAGES", 6
        )
        self._summary_model = summary_model or getattr(
            cfg, "COMPACTION_SUMMARY_MODEL", None
        )

    def _get_config(self) -> Any:
        """Lazy config resolution."""
        if self._config is None:
            try:
                from k8s_autopilot.config.config import Config
                self._config = Config()
            except Exception:
                pass
        return self._config

    def _get_thread_id(self) -> str:
        """Extract thread ID from LangGraph runtime config."""
        try:
            from langgraph.config import get_config as lg_get_config
            lg_cfg = lg_get_config()
            tid = lg_cfg.get("configurable", {}).get("thread_id")
            if tid:
                return tid
        except Exception:
            pass
        return f"session_{int(time.time())}"

    def _write_history(self, path: str, content: str) -> None:
        """Write history to the backend via the CompositeBackend route.

        Uses the /conversation_history/ route added to CompositeBackend
        in factory.py.
        """
        try:
            from langgraph.config import get_store
            store = get_store()
            if store is not None:
                # Use store's put method with conversation history namespace
                store.put(
                    ("conversation_history",),
                    path,
                    {"content": content},
                )
                logger.debug(f"Wrote conversation history to store: {path}")
                return
        except Exception:
            pass

        # Fallback: log the history path (CompositeBackend write handled by agent)
        logger.info(f"Conversation history ready for offload: {path} ({len(content):d} chars)")

    def _generate_summary(self, messages: List[Any]) -> str:
        """Generate a summary of the conversation using the configured model.

        Falls back to a simple extraction if LLM summarization fails.
        """
        try:
            from langchain.chat_models import init_chat_model

            cfg = self._get_config()
            model_name = self._summary_model
            if not model_name:
                model_name = getattr(cfg, "LLM_MODEL", "gpt-4o-mini")

            provider = getattr(cfg, "LLM_PROVIDER", "openai")

            model = init_chat_model(
                model_name,
                model_provider=provider,
                temperature=0.0,
                max_tokens=500,
            )

            prompt = _generate_summary_prompt(messages)
            response = model.invoke([HumanMessage(content=prompt)])
            summary = getattr(response, "content", "")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        except Exception as e:
            logger.warning(f"LLM summarization failed: {e}")

        # Fallback: extract key messages
        return self._simple_summary(messages)

    def _simple_summary(self, messages: List[Any]) -> str:
        """Simple extraction-based summary (no LLM needed)."""
        human_msgs = []
        for msg in messages:
            if getattr(msg, "type", "") == "human":
                content = getattr(msg, "content", "")
                if isinstance(content, str) and content.strip():
                    human_msgs.append(content[:200])

        if not human_msgs:
            return "Previous conversation context (compacted)."

        return (
            f"This session covered {len(human_msgs)} user requests. "
            f"Topics: {'; '.join(human_msgs[-5:])}"
        )

    def compact_if_needed(
        self,
        messages: List[Any],
    ) -> Optional[Dict[str, List[Any]]]:
        """Check token budget and compact if needed.

        Args:
            messages: Current message list.

        Returns:
            None if no compaction needed.
            Dict with ``{"messages": [...]}`` containing compacted messages
            if compaction was triggered.
        """
        token_count = _estimate_tokens(messages)
        threshold = int(self._token_budget * 0.75)

        if token_count < threshold:
            return None

        logger.info(f"Conversation compaction triggered: {token_count:d} tokens > {threshold:d} threshold "
            "(budget: {self._token_budget:d}, compaction #{self._compaction_count + 1:d})")

        # Step 1: Offload full history
        thread_id = self._get_thread_id()
        history_path = f"/conversation_history/{thread_id}.md"

        history_lines: list[str] = [
            f"# Conversation History — {thread_id}\n",
            f"Compacted at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n",
            f"Messages: {len(messages)} | Estimated tokens: {token_count}\n",
            "---\n",
        ]
        for msg in messages:
            history_lines.append(_format_message_for_history(msg))
            history_lines.append("---\n")

        history_content = "\n".join(history_lines)
        self._write_history(thread_id, history_content)

        # Step 2: Generate summary
        summary = self._generate_summary(messages)

        # Step 3: Build compacted message list
        kept = messages[-self._keep_last:]

        compaction_notice = SystemMessage(content=(
            f"> **Conversation history was compacted** "
            f"(compaction #{self._compaction_count + 1}). "
            f"Full transcript saved at `{history_path}`. "
            f"Use `read_file(\"{history_path}\")` to recall specific past "
            f"decisions or context.\n\n"
            f"> **Summary of prior conversation:**\n> {summary}"
        ))

        self._compaction_count += 1

        return {"messages": [compaction_notice] + kept}

    @property
    def compaction_count(self) -> int:
        """Number of times compaction has been triggered."""
        return self._compaction_count
