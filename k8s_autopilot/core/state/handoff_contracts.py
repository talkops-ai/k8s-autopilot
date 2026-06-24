"""Typed envelopes for bidirectional coordinator handoffs.

These contracts define the structured data that flows between coordinators
when one domain needs assistance from another.  The supervisor_router reads
these envelopes to make deterministic push/pop decisions on the dialog_state
stack.

Reference: ``docs/k8s_autopilot_architecture_spec.md``
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, TypedDict

from typing_extensions import NotRequired


# ---------------------------------------------------------------------------
# Handoff Request — coordinator A asks for help from coordinator B
# ---------------------------------------------------------------------------

class HandoffRequest(TypedDict):
    """Envelope a coordinator returns when it needs another domain's help.

    The supervisor intercepts this, pushes the target onto ``dialog_state``,
    and routes execution to the target coordinator.
    """

    source_agent: str
    """Who is asking (e.g. ``"helm_agent"``)."""

    target_agent: str
    """Who should handle it (e.g. ``"k8s_ops_agent"``)."""

    intent: str
    """Human-readable action (e.g. ``"fetch_runtime_logs"``)."""

    payload: Dict[str, Any]
    """Domain-specific data the target needs."""

    return_to: str
    """Agent to return control to after the target finishes."""

    resume_cursor: str
    """Phase/step the caller was at, so it can resume cleanly."""

    correlation_id: str
    """UUID linking this request to the eventual HandoffResult."""


# ---------------------------------------------------------------------------
# Handoff Result — coordinator B returns its findings to coordinator A
# ---------------------------------------------------------------------------

class HandoffResult(TypedDict):
    """Envelope a coordinator (the callee) writes when it finishes a
    cross-domain task requested via :class:`HandoffRequest`.

    The supervisor pops the callee off ``dialog_state`` and routes back
    to the caller with this result attached.
    """

    source_agent: str
    """Who fulfilled the request (= the callee)."""

    target_agent: str
    """Who asked (= ``return_to`` from the original request)."""

    correlation_id: str
    """Must match the correlation_id from the corresponding HandoffRequest."""

    status: str
    """``"completed"`` | ``"error"`` | ``"partial"``."""

    summary: str
    """Compact text summary suitable for the supervisor LLM."""

    payload: Dict[str, Any]
    """Structured result data (e.g. pod status, logs, metrics)."""

    artifact_refs: NotRequired[Dict[str, str]]
    """Optional references to large outputs (paths, URIs)."""


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def create_handoff_request(
    *,
    source_agent: str,
    target_agent: str,
    intent: str,
    payload: Dict[str, Any] | None = None,
    resume_cursor: str = "continue",
) -> HandoffRequest:
    """Build a validated :class:`HandoffRequest`.

    ``return_to`` defaults to ``source_agent`` and a fresh ``correlation_id``
    is generated automatically.
    """
    return HandoffRequest(
        source_agent=source_agent,
        target_agent=target_agent,
        intent=intent,
        payload=payload or {},
        return_to=source_agent,
        resume_cursor=resume_cursor,
        correlation_id=str(uuid.uuid4()),
    )


def create_handoff_result(
    *,
    request: HandoffRequest,
    source_agent: str,
    status: str = "completed",
    summary: str = "",
    payload: Dict[str, Any] | None = None,
    artifact_refs: Dict[str, str] | None = None,
) -> HandoffResult:
    """Build a :class:`HandoffResult` that references the originating request.

    The ``correlation_id`` is copied from *request* to ensure traceability.
    """
    result = HandoffResult(
        source_agent=source_agent,
        target_agent=request["return_to"],
        correlation_id=request["correlation_id"],
        status=status,
        summary=summary,
        payload=payload or {},
    )
    if artifact_refs:
        result["artifact_refs"] = artifact_refs
    return result


def _extract_clean_text(content: Any) -> str:
    """Extract user-facing text from content that may be a string or
    a list of Gemini/Anthropic content blocks.

    Strips thinking blocks and signature metadata — returns only 'text'
    type blocks concatenated.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else str(content)
    return str(content) if content else ""


def format_handoff_for_context(result: Dict[str, Any]) -> str:
    """Format a :class:`HandoffResult` as a compact string for injection
    into the supervisor LLM or coordinator context.

    Example output::

        [Cross-domain result] k8s_ops_agent → helm_agent (completed)
        Summary: Pods in namespace nginx are healthy (3/3 ready).
    """
    header = (
        f"[Cross-domain result] {result['source_agent']} → "
        f"{result['target_agent']} ({result['status']})"
    )
    raw_summary = result.get("summary", "No summary provided.")
    body = _extract_clean_text(raw_summary)
    return f"{header}\nSummary: {body}"


# ---------------------------------------------------------------------------
# Loop-guard helpers
# ---------------------------------------------------------------------------

_DEFAULT_MAX_HANDOFFS = 5  # per correlation_id


def check_loop_guard(
    loop_guard: Dict[str, Any] | None,
    correlation_id: str,
    max_handoffs: int = _DEFAULT_MAX_HANDOFFS,
) -> bool:
    """Return ``True`` if the correlation_id has exceeded max handoffs.

    The supervisor_router calls this before every push to detect
    infinite-loop patterns.
    """
    if not loop_guard:
        return False
    count = loop_guard.get(correlation_id, 0)
    return count >= max_handoffs


def increment_loop_guard(
    loop_guard: Dict[str, Any] | None,
    correlation_id: str,
) -> Dict[str, Any]:
    """Increment the handoff count for a correlation_id."""
    guard = dict(loop_guard or {})
    guard[correlation_id] = guard.get(correlation_id, 0) + 1
    return guard


# ---------------------------------------------------------------------------
# Text-based handoff inference (Industry Standard pattern)
# ---------------------------------------------------------------------------

_HANDOFF_PATTERNS: tuple[str, ...] = (
    "use the k8s operator",
    "use the kubernetes operator",
    "use the kubernetes assistant",
    "use the helm operator",
    "use the app operator",
    "use the observability operator",
    "use the prometheus operator",
    "use the alertmanager operator",
    "use the k8s-operator",
    "use the k8s cluster",
    "k8s operator to inspect",
    "k8s operator can",
    "helm operator to",
    "app operator to",
    "observability operator to",
    "handoff_required",
    "outside my scope",
    "outside of my scope",
    "beyond my scope",
    "not within my capabilities",
    "not within my scope",
    "falls outside my",
    "use the appropriate operator",
    "please use the appropriate operator",
    "another operator",
    "different domain",
)

_HANDOFF_TARGET_KEYWORDS: Dict[str, str] = {
    "k8s": "k8s_ops_agent", "kubernetes": "k8s_ops_agent",
    "pod": "k8s_ops_agent", "deployment": "k8s_ops_agent",
    "namespace": "k8s_ops_agent", "service": "k8s_ops_agent",
    "helm": "helm_agent", "chart": "helm_agent",
    "argocd": "app_mgmt_agent", "argo": "app_mgmt_agent",
    "traefik": "app_mgmt_agent", "rollout": "app_mgmt_agent",
    "prometheus": "observability_agent", "alertmanager": "observability_agent",
    "metrics": "observability_agent", "alerts": "observability_agent",
    "loki": "observability_agent", "observability": "observability_agent",
}

def extract_handoff_from_text(
    message: Any,
    source_agent: str,
    payload: Dict[str, Any] | None = None,
) -> HandoffRequest | None:
    """Detect if the text contains a handoff refusal, and build a HandoffRequest.
    
    This is called by coordinator output_transforms to natively yield handoffs.
    """
    # Normalize content — Gemini may return list-of-blocks instead of str
    if isinstance(message, list):
        message = "".join(
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
            else (str(b) if isinstance(b, str) else "")
            for b in message
        )
    if not isinstance(message, str):
        message = str(message) if message else ""
    text_lower = message.lower()
    if not any(p in text_lower for p in _HANDOFF_PATTERNS):
        return None

    # Parse context
    user_request = "cross-domain request"
    prior_context = ""
    
    if "User Request:" in message:
        parts = message.split("User Request:", 1)
        req_part = parts[1]
        user_request = (
            req_part.split("Context:")[0].strip()
            if "Context:" in req_part else req_part.strip()
        )
    if "Context:" in message:
        prior_context = message.split("Context:", 1)[1].strip()

    combined_text = (user_request + " " + prior_context).lower()
    target_agent = "k8s_ops_agent"  # fallback
    for kw, node in _HANDOFF_TARGET_KEYWORDS.items():
        if kw in combined_text and node != source_agent:
            target_agent = node
            break
            
    # Make sure we don't hand off to ourselves
    if target_agent == source_agent:
        fallback_nodes = ["k8s_ops_agent", "helm_agent", "app_mgmt_agent", "observability_agent"]
        target_agent = next((n for n in fallback_nodes if n != source_agent), "k8s_ops_agent")

    payload_data: Dict[str, Any] = {
        "user_request": user_request,
        "prior_context": prior_context,
    }
    if payload and payload.get("domain_summary"):
        payload_data["domain_summary"] = payload["domain_summary"]

    return create_handoff_request(
        source_agent=source_agent,
        target_agent=target_agent,
        intent=user_request,
        payload=payload_data,
        resume_cursor="post_handoff",
    )
