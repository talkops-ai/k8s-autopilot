"""
Supervisor Agent — Context Engineering Middleware.

Provides a two-layer middleware stack for the supervisor router that:
  1. Re-injects domain summaries (cross-domain awareness)
  2. Auto-summarizes conversation history (cost + context control)
  3. Caps model calls (runaway-loop safety)

Usage::

    from k8s_autopilot.core.agents.supervisor_middleware import (
        build_supervisor_middleware,
    )

    middleware = build_supervisor_middleware(config)
    agent = create_agent(
        ...,
        middleware=middleware,
    )

Industry standards applied:
  - Trigger at ~75% of effective context budget (not raw model limit)
  - Keep last 4–6 messages (most recent 2–3 coordinator round-trips)
  - Use the cheapest available model tier for summarization
  - Separate "LLM context" from "UI history" (permanent state mutation ok)

Reference: LangChain v1 SummarizationMiddleware, context-engineering docs,
           helm_operator/middleware.py OperationContextMiddleware pattern.
"""

import base64
import os
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, SystemMessage

from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("SupervisorMiddleware")

# ---------------------------------------------------------------------------
# Default thresholds (overridable via env vars or Config)
# ---------------------------------------------------------------------------
#
# Industry standard: trigger summarization at ~75% of the context budget
# allocated for conversation messages.  The supervisor's system prompt is
# ~800 tokens, leaving ~7-8K effective budget on conservative models.
#
# For the supervisor (a lightweight router), 4000 tokens ≈ 3-4 full
# coordinator round-trips.  After that, older messages are summarized.
#
# Messages to keep: industry best practice is 4-6 messages (last 2-3
# user/coordinator round-trips).  Fewer messages keeps the context tight
# for a router agent that doesn't need deep conversation history.

_SUMMARIZATION_TRIGGER_TOKENS = int(
    os.getenv("SUPERVISOR_SUMMARIZATION_TRIGGER_TOKENS", "4000")
)

_SUMMARIZATION_KEEP_MESSAGES = int(
    os.getenv("SUPERVISOR_SUMMARIZATION_KEEP_MESSAGES", "6")
)

_MODEL_CALL_LIMIT = int(
    os.getenv("SUPERVISOR_MODEL_CALL_LIMIT", "15")
)


# ---------------------------------------------------------------------------
# K8s-domain-aware summarization prompt
# ---------------------------------------------------------------------------

SUPERVISOR_SUMMARY_PROMPT = """\
You are a summarization assistant for a Kubernetes infrastructure management \
supervisor agent.  Your job is to compress conversation history into a concise \
summary that preserves routing-critical information.

**PRESERVE these details in the summary:**
- Which coordinators were invoked (helm_operator, k8s_operator, app_operator, \
observability_operator) and their outcomes (success/failure/pending)
- Cross-domain handoff context (if one coordinator deferred to another)
- Key user intents and what was requested
- Active workflow state and phase
- Any errors or retries that occurred
- Resource identifiers: chart names, release names, namespaces, app names, \
alertnames

**DISCARD:**
- Verbose tool call arguments and raw JSON payloads
- Intermediate routing decisions that were superseded
- Duplicate information already captured in a prior summary

**FORMAT:**
Write a compact paragraph (3-8 sentences). Start with the most recent action. \
Use the pattern: "User requested X → routed to Y coordinator → outcome was Z."

**Messages to summarize:**
{messages}
"""


# ---------------------------------------------------------------------------
# Layer 1: SupervisorContextMiddleware — domain summaries injection
# ---------------------------------------------------------------------------

class SupervisorContextMiddleware(AgentMiddleware):
    """Re-injects cross-domain context before every supervisor model call.

    Reads ``domain_summaries`` from ``MainSupervisorState`` and prepends a
    compact SystemMessage so the supervisor always has awareness of what
    each coordinator accomplished — even after older messages are summarized.

    This follows the same pattern as ``OperationContextMiddleware`` in
    ``helm_operator/middleware.py``, adapted for the supervisor layer.

    Usage::

        middleware = [SupervisorContextMiddleware(), ...]
        agent = create_agent(middleware=middleware, ...)
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> dict[str, Any] | None:
        """Read domain summaries from state and inject as SystemMessage."""
        raw = state.get("domain_summaries", [])
        domain_summaries = raw if isinstance(raw, list) else []

        if not domain_summaries:
            return None

        # Build compact summary from domain_summaries entries
        lines: list[str] = []
        for summary in domain_summaries:
            if not isinstance(summary, dict):
                continue
            domain = summary.get("domain", "unknown")
            outcome = summary.get("outcome", "completed")
            detail = summary.get("detail", "")
            if detail:
                lines.append(f"- **{domain}**: {outcome} — {detail}")
            else:
                lines.append(f"- **{domain}**: {outcome}")

        if not lines:
            return None

        context_text = (
            "## Cross-Domain Context (auto-injected, survives "
            "summarization)\n"
            "Previous coordinator outcomes this session:\n"
            + "\n".join(lines)
            + "\n\nUse this context when routing follow-up requests."
        )

        logger.debug(
            "SupervisorContextMiddleware: injecting domain summaries",
            extra={
                "summary_count": len(lines),
                "context_length": len(context_text),
            },
        )

        return {
            "messages": [SystemMessage(content=context_text)],
        }

    async def abefore_model(
        self, state: AgentState, runtime: Any,
    ) -> dict[str, Any] | None:
        """Async version — delegates to sync implementation."""
        return self.before_model(state, runtime)


# ---------------------------------------------------------------------------
# Layer 0: ThoughtSignatureFixMiddleware — Gemini checkpoint resume fix
# ---------------------------------------------------------------------------


_FUNCTION_CALL_THOUGHT_SIGNATURES_MAP_KEY = (
    "__gemini_function_call_thought_signatures__"
)

# The Gemini API accepts this special bypass value to skip strict
# thought-signature validation on replayed tool-call history.
_BYPASS_SIGNATURE = base64.b64encode(
    b"skip_thought_signature_validator"
).decode("utf-8")


class ThoughtSignatureFixMiddleware(AgentMiddleware):
    """Fix stale Gemini thought signatures on checkpoint resume.

    Gemini 3.x models embed ``thought_signature`` bytes in function-call
    parts.  These signatures are session-specific: when a LangGraph
    checkpoint replays the message history on resume, the stale signatures
    cause Gemini to reject with::

        400 Bad Request — Thought signature is not valid.

    This middleware patches **AIMessages that have tool_calls** by
    injecting the ``skip_thought_signature_validator`` bypass string into
    ``additional_kwargs``.  This tells the Gemini adapter to skip strict
    signature validation during history replay.

    **Provider-agnostic:** The middleware only patches messages whose
    ``response_metadata["model_provider"]`` is ``"google_genai"`` (or
    when tool calls are present and a Gemini model was used).  For
    other providers (OpenAI, Anthropic, etc.) it is a no-op.

    MUST be the **first** middleware so it runs before the model sees
    the messages.
    """

    def before_model(
        self, state: AgentState, runtime: Any,
    ) -> dict[str, Any] | None:
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
    ) -> dict[str, Any] | None:
        """Async version — delegates to sync implementation."""
        return self.before_model(state, runtime)



# ---------------------------------------------------------------------------
# Factory: build_supervisor_middleware
# ---------------------------------------------------------------------------

def build_supervisor_middleware(
    config: "Config | None" = None,
    *,
    summarization_trigger_tokens: int | None = None,
    summarization_keep_messages: int | None = None,
    model_call_limit: int | None = None,
) -> list[Any]:
    """Assemble the middleware stack for the supervisor agent.

    Uses the user's configured LLM provider for summarization (cheapest
    available tier — ``llm_config`` / standard tier).  No hardcoded model
    names.

    Args:
        config: Application config for dynamic model/threshold resolution.
        summarization_trigger_tokens: Override token trigger threshold.
        summarization_keep_messages: Override messages to keep.
        model_call_limit: Override model call limit.

    Returns:
        A list of middleware instances for ``create_agent(middleware=...)``.
    """
    from langchain.agents.middleware import (
        ModelCallLimitMiddleware,
        SummarizationMiddleware,
    )

    middleware: list[Any] = []

    # ── 0. Strip stale Gemini thought signatures ──────────────────────
    # MUST be first — runs before any model sees the messages.
    middleware.append(ThoughtSignatureFixMiddleware())
    logger.info("Middleware: ThoughtSignatureFixMiddleware (before_model)")

    # ── 1. Domain context injection (survives summarization) ──────────
    middleware.append(SupervisorContextMiddleware())
    logger.info("Middleware: SupervisorContextMiddleware (before_model)")

    # ── 2. Summarization — auto-compress older messages ───────────────
    #
    # Resolve the summarization model from config.  The supervisor uses
    # the standard LLM tier (llm_config) which is the cheapest configured
    # model.  We pass the model string to SummarizationMiddleware which
    # handles init_chat_model() internally.
    trigger_tokens = summarization_trigger_tokens
    keep_messages = summarization_keep_messages
    mc_limit_override = model_call_limit

    # Resolve from Config (respects runtime overrides → env vars → defaults)
    if config is not None:
        if trigger_tokens is None:
            trigger_tokens = config.get(
                "SUPERVISOR_SUMMARIZATION_TRIGGER_TOKENS",
                _SUMMARIZATION_TRIGGER_TOKENS,
            )
        if keep_messages is None:
            keep_messages = config.get(
                "SUPERVISOR_SUMMARIZATION_KEEP_MESSAGES",
                _SUMMARIZATION_KEEP_MESSAGES,
            )
        if mc_limit_override is None:
            mc_limit_override = config.get(
                "SUPERVISOR_MODEL_CALL_LIMIT",
                _MODEL_CALL_LIMIT,
            )

    # Final fallback to env-var-derived module defaults
    trigger_tokens = trigger_tokens or _SUMMARIZATION_TRIGGER_TOKENS
    keep_messages = keep_messages or _SUMMARIZATION_KEEP_MESSAGES

    summarization_model: str | None = None
    if config is not None:
        try:
            llm_cfg = config.get_llm_config()
            # Extract the model string (e.g. "google_genai:gemini-2.0-flash",
            # "gpt-4o-mini", etc.) — SummarizationMiddleware accepts this
            # directly via init_chat_model().
            summarization_model = llm_cfg.get("model")
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not resolve summarization model from config — "
                "SummarizationMiddleware will use its default",
            )

    summarization_kwargs: dict[str, Any] = {
        "trigger": ("tokens", trigger_tokens),
        "keep": ("messages", keep_messages),
        "summary_prompt": SUPERVISOR_SUMMARY_PROMPT,
    }

    if summarization_model:
        summarization_kwargs["model"] = summarization_model

    middleware.append(SummarizationMiddleware(**summarization_kwargs))
    logger.info(
        "Middleware: SummarizationMiddleware",
        extra={
            "trigger_tokens": trigger_tokens,
            "keep_messages": keep_messages,
            "model": summarization_model or "default",
        },
    )

    # ── 3. Model call limit — prevent runaway routing loops ───────────
    mc_limit = mc_limit_override or _MODEL_CALL_LIMIT
    middleware.append(
        ModelCallLimitMiddleware(
            run_limit=mc_limit,
            exit_behavior="end",  # Graceful stop instead of exception
        ),
    )
    logger.info(
        "Middleware: ModelCallLimitMiddleware",
        extra={"run_limit": mc_limit},
    )

    logger.info(
        "Supervisor middleware stack assembled",
        extra={
            "total_middleware": len(middleware),
            "types": [type(m).__name__ for m in middleware],
        },
    )

    return middleware
