"""Thought signature fix middleware for Gemini checkpoints resume."""

import base64
from typing import Any, Dict
from langchain_core.messages import AIMessage
from langchain.agents.middleware import AgentState

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("ThoughtSignatureFix")

_FUNCTION_CALL_THOUGHT_SIGNATURES_MAP_KEY = (
    "__gemini_function_call_thought_signatures__"
)

# The Gemini API accepts this special bypass value to skip strict
# thought-signature validation on replayed tool-call history.
_BYPASS_SIGNATURE = base64.b64encode(
    b"skip_thought_signature_validator"
).decode("utf-8")


@register_middleware(name="thought_signature_fix")
class ThoughtSignatureFixMiddleware(BaseAgentMiddleware):
    """Fix stale Gemini thought signatures on checkpoint resume.

    Gemini 3.x models embed ``thought_signature`` bytes in function-call
    parts. These signatures are session-specific: when a LangGraph
    checkpoint replays the message history on resume, the stale signatures
    cause Gemini to reject with Bad Request.
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        patched: list[Any] = []
        changed = False

        for msg in messages:
            if not isinstance(msg, AIMessage):
                patched.append(msg)
                continue

            # Only patch AIMessages with tool calls
            if not msg.tool_calls:
                patched.append(msg)
                continue

            # Provider guard: only applies to Google GenAI models
            provider = (msg.response_metadata or {}).get("model_provider", "")
            model_name = (msg.response_metadata or {}).get("model_name", "")
            is_google = (
                provider == "google_genai"
                or "gemini" in model_name.lower()
            )
            if not is_google:
                patched.append(msg)
                continue

            # Check if we already have the bypass signature
            existing_sigs = (msg.additional_kwargs or {}).get(
                _FUNCTION_CALL_THOUGHT_SIGNATURES_MAP_KEY, {}
            )
            all_bypassed = existing_sigs and all(
                v == _BYPASS_SIGNATURE for v in existing_sigs.values()
            )
            if all_bypassed:
                patched.append(msg)
                continue

            # Build the bypass signature map for all tool calls
            bypass_map = {}
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                if tc_id:
                    bypass_map[tc_id] = _BYPASS_SIGNATURE

            if not bypass_map:
                patched.append(msg)
                continue

            # Also strip thinking/reasoning signatures from content blocks
            new_content: str | list[Any] = msg.content
            if isinstance(msg.content, list):
                new_content = []
                for block in msg.content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype in ("thinking", "reasoning"):
                            b = {
                                k: v for k, v in block.items()
                                if k != "signature"
                            }
                            extras = b.get("extras")
                            if isinstance(extras, dict) and "signature" in extras:
                                b["extras"] = {
                                    k: v for k, v in extras.items()
                                    if k != "signature"
                                }
                                new_content.append(b)
                        elif btype == "text":
                            extras = block.get("extras")
                            if isinstance(extras, dict) and "signature" in extras:
                                b = dict(block)
                                b["extras"] = {
                                    k: v for k, v in extras.items()
                                    if k != "signature"
                                }
                                new_content.append(b)
                            else:
                                new_content.append(block)
                        else:
                            new_content.append(block)
                    else:
                        new_content.append(block)

            new_kwargs = dict(msg.additional_kwargs or {})
            new_kwargs[_FUNCTION_CALL_THOUGHT_SIGNATURES_MAP_KEY] = bypass_map

            patched.append(
                msg.model_copy(
                    update={
                        "additional_kwargs": new_kwargs,
                        "content": new_content,
                    },
                ),
            )
            changed = True
            logger.debug(
                "ThoughtSignatureFixMiddleware: injected bypass signature",
                extra={
                    "tool_call_ids": list(bypass_map.keys()),
                    "model_name": model_name,
                },
            )

        if not changed:
            return None

        return {"messages": patched}

    async def abefore_model(
        self, state: AgentState, runtime: Any,
    ) -> Dict[str, Any] | None:
        """Async version — delegates to sync implementation."""
        return self.before_model(state, runtime)
