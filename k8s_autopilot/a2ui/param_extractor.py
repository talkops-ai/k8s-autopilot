"""Parameter extraction for HITL approval cards.

Converts ``action_requests`` from ``HumanInTheLoopMiddleware`` into
a flat list of ``{key, value}`` display pairs that the frontend
``hitlApprovalCard`` renders as a read-only parameter grid.

This is the **single source of truth** for parameter display across
all domain approval components (Helm, K8s, App Operator, Observability).

Design decisions:
  - Snake_case arg keys → Title Case labels (``release_name`` → ``Release Name``)
  - Internal/meta keys are stripped (``__tool_call_id``, ``id``, ``type``)
  - Non-string values → human-readable strings (lists → comma-sep, dicts → JSON)
  - Flat structure — no nested parameter groups
"""

from __future__ import annotations

import json
from typing import Any

# Keys that are internal/metadata and should not be displayed
_SKIP_KEYS: frozenset[str] = frozenset({
    "__tool_call_id",
    "id",
    "type",
    "tool_call_id",
})


def _key_to_label(key: str) -> str:
    """Convert a snake_case key to a Title Case label.

    Examples::

        >>> _key_to_label("release_name")
        'Release Name'
        >>> _key_to_label("chart_name")
        'Chart Name'
        >>> _key_to_label("apiVersion")
        'Api Version'
    """
    # Handle camelCase by inserting spaces before uppercase letters
    spaced = ""
    for i, ch in enumerate(key):
        if ch.isupper() and i > 0 and key[i - 1].islower():
            spaced += " "
        spaced += ch

    return spaced.replace("_", " ").strip().title()


def _value_to_str(value: Any) -> str:
    """Convert an arbitrary value to a display string.

    - ``None`` → ``"—"``
    - ``bool`` → ``"Yes"`` / ``"No"``
    - ``list`` → comma-separated
    - ``dict`` → compact JSON
    - Everything else → ``str(value)``
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        if not value:
            return "—"
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        if not value:
            return "—"
        return json.dumps(value, indent=2, default=str)
    return str(value)


def extract_parameters(
    action_requests: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, str]]]:
    """Extract display parameters from ``action_requests``.

    Processes the first action request (primary operation) and
    extracts its tool args as key-value display pairs.

    Args:
        action_requests: List of action_request dicts from the
            ``HumanInTheLoopMiddleware``. Each has ``name`` (str),
            ``args`` (dict), and optionally ``description`` (str).

    Returns:
        A 3-tuple of:
          - ``tool_name``: The primary tool name (e.g. ``"helm_install_chart"``).
          - ``tool_description``: The middleware-generated ``description``
            if present, otherwise a generated label.
          - ``parameters``: List of ``{"key": "Release Name", "value": "nginx-web"}``
            dicts suitable for the frontend parameter grid.

    Example::

        >>> reqs = [{"name": "helm_install_chart", "args": {
        ...     "chart_name": "bitnami/nginx",
        ...     "release_name": "nginx-web",
        ...     "namespace": "production",
        ... }}]
        >>> tool, desc, params = extract_parameters(reqs)
        >>> tool
        'helm_install_chart'
        >>> params[0]
        {'key': 'Chart Name', 'value': 'bitnami/nginx'}
    """
    if not action_requests:
        return "", "", []

    # Process the primary action request
    primary = action_requests[0]
    if not isinstance(primary, dict):
        return "", "", []

    tool_name = primary.get("name", "")
    description = primary.get("description", "")
    args = primary.get("args", {})

    if not isinstance(args, dict):
        args = {}

    # Generate a fallback description from the tool name
    if not description:
        description = _key_to_label(tool_name) if tool_name else ""

    # Extract parameters from args
    parameters: list[dict[str, str]] = []
    for key, value in args.items():
        if key in _SKIP_KEYS:
            continue
        parameters.append({
            "key": _key_to_label(key),
            "value": _value_to_str(value),
        })

    # If there are multiple action requests, add a summary count
    if len(action_requests) > 1:
        additional = len(action_requests) - 1
        parameters.append({
            "key": "Additional Operations",
            "value": f"+{additional} more",
        })

    return tool_name, description, parameters
