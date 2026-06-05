"""Schema loader utility for fixed A2UI schemas.

Loads pre-authored JSON component schemas from the ``schemas/`` directory.
These schemas define the component tree at author-time; the agent only
streams *data* into the data model at runtime.

Usage::

    from k8s_autopilot.core.a2ui.schema_loader import load_schema, SCHEMAS

    # Load by name (no extension)
    components = load_schema("helm_release_card")

    # Or use the pre-loaded dict
    components = SCHEMAS["approval_card"]

Reference:
    CopilotKit pattern — ``a2ui.load_schema("flight_schema.json")``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).parent / "schemas"


def load_schema(name: str) -> list[dict[str, Any]]:
    """Load a fixed A2UI component schema from a JSON file.

    Parameters
    ----------
    name : str
        Schema name (with or without ``.json`` extension).
        E.g. ``"helm_release_card"`` or ``"helm_release_card.json"``.

    Returns
    -------
    list[dict]
        The component list ready for ``a2ui.update_components()``.

    Raises
    ------
    FileNotFoundError
        If the schema file does not exist.
    """
    if not name.endswith(".json"):
        name = f"{name}.json"

    path = _SCHEMAS_DIR / name
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    logger.debug("Loaded A2UI schema %s (%d components)", name, len(data))
    return data


def _load_all_schemas() -> dict[str, list[dict[str, Any]]]:
    """Pre-load all ``.json`` files in the schemas directory.

    Returns a dict mapping schema name (without extension) → component list.
    """
    schemas: dict[str, list[dict[str, Any]]] = {}
    if not _SCHEMAS_DIR.is_dir():
        logger.warning("Schemas directory not found: %s", _SCHEMAS_DIR)
        return schemas

    for path in sorted(_SCHEMAS_DIR.glob("*.json")):
        try:
            schemas[path.stem] = load_schema(path.name)
        except Exception:
            logger.exception("Failed to load schema %s", path.name)

    return schemas


# Pre-loaded schemas — available at import time
SCHEMAS: dict[str, list[dict[str, Any]]] = _load_all_schemas()
