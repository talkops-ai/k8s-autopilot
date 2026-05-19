from typing import Any, Dict, Optional

def extract_domain_summary(
    domain: str,
    final_message: Optional[str],
) -> Dict[str, Any]:
    """Extract a compact domain summary from the coordinator's final message.

    The summary is stored in the supervisor's state and passed to downstream
    coordinators so they have cross-domain awareness without reading full
    message histories.

    Args:
        domain: The domain name (e.g., ``"observability"``, ``"k8s"``).
        final_message: The coordinator's final output message.

    Returns:
        A dict with domain, message_preview, and a flag indicating whether
        the output contained a cross-domain handoff signal.
    """
    msg_raw = final_message or ""
    
    # Extract string if content is a list of blocks
    if isinstance(msg_raw, list):
        msg_parts = []
        for b in msg_raw:
            if isinstance(b, dict) and b.get("type") == "text":
                msg_parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                msg_parts.append(b)
            else:
                msg_parts.append(str(b))
        msg = "".join(msg_parts)
    elif not isinstance(msg_raw, str):
        msg = str(msg_raw)
    else:
        msg = msg_raw

    is_handoff = "outside my scope" in msg.lower()

    summary: Dict[str, Any] = {
        "domain": domain,
        "message_preview": msg[:300] if msg else "",
        "is_handoff": is_handoff,
    }

    # Extract service names mentioned in common patterns
    # (lightweight heuristic — coordinator should write /shared/ for full detail)
    if "service" in msg.lower() or "namespace" in msg.lower():
        summary["has_service_context"] = True

    return summary
