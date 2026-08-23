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
# Factory: build_supervisor_middleware
# ---------------------------------------------------------------------------

def build_supervisor_middleware(
    config: "Config | None" = None,
    *,
    summarization_trigger_tokens: int | None = None,
    summarization_keep_messages: int | None = None,
    model_call_limit: int | None = None,
) -> list[Any]:
    """Assemble the middleware stack for the supervisor agent."""
    from langchain.agents.middleware import (
        ModelCallLimitMiddleware,
        SummarizationMiddleware,
    )
    from k8s_autopilot.core.middleware.registry import get_middleware_registry

    middleware: list[Any] = []

    # Load custom middlewares from central registry (ThoughtSignatureFix, SupervisorContext)
    custom_mws = get_middleware_registry().build_middlewares(
        ["thought_signature_fix", "supervisor_context"],
        config=config,
    )
    middleware.extend(custom_mws)

    # ── 2. Summarization — auto-compress older messages ───────────────
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
