"""User identity resolution and Kubernetes RBAC mapping.

Implements the ``slack_user_id → internal_user_id → cluster_rbac``
pipeline described in the architecture docs.

Identity mappings are loaded from:

1. ``IDENTITY_MAPPING_FILE`` env var — path to a JSON file.
2. ``IDENTITY_MAPPING`` env var — inline JSON string.
3. Default: all unmapped users receive the ``"viewer"`` role.

Example mapping file::

    {
        "slack:U12345ABC": {
            "internal_id": "alice@company.com",
            "display_name": "Alice",
            "rbac_role": "admin",
            "namespaces": ["default", "staging", "production"],
            "operations": ["read", "deploy", "delete", "scale"]
        },
        "slack:U67890DEF": {
            "internal_id": "bob@company.com",
            "display_name": "Bob",
            "rbac_role": "operator",
            "namespaces": ["default", "staging"],
            "operations": ["read", "deploy"]
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UserIdentity:
    """Resolved user identity with Kubernetes RBAC context."""

    platform: str
    """Source platform (``"slack"``, ``"teams"``, etc.)."""

    platform_user_id: str
    """Platform-native user identifier."""

    internal_user_id: str
    """Internal / corporate user identifier (email, employee ID, etc.)."""

    display_name: str
    """Human-readable display name."""

    rbac_role: str
    """Kubernetes RBAC role: ``"admin"``, ``"operator"``, ``"viewer"``."""

    allowed_namespaces: list[str] = field(default_factory=lambda: ["*"])
    """Kubernetes namespaces this user can operate on.
    ``["*"]`` means all namespaces."""

    allowed_operations: list[str] = field(default_factory=lambda: ["read"])
    """Permitted operations: ``"read"``, ``"deploy"``, ``"delete"``,
    ``"scale"``, ``"rollback"``, etc."""


class IdentityMapper:
    """Resolves platform users to :class:`UserIdentity` with RBAC context.

    The mapper is platform-agnostic — it uses composite keys of the form
    ``"platform:user_id"`` (e.g. ``"slack:U12345ABC"``).
    """

    def __init__(self) -> None:
        self._mapping: dict[str, dict[str, Any]] = {}
        self._load_mapping()

    # ── Loading ───────────────────────────────────────────────────────

    def _load_mapping(self) -> None:
        """Load identity mapping from env var sources."""
        # Priority 1: JSON file
        mapping_file = os.getenv("IDENTITY_MAPPING_FILE")
        if mapping_file:
            try:
                with open(mapping_file) as fh:
                    self._mapping = json.load(fh)
                    logger.info(
                        "Loaded identity mapping from file: %s (%d entries)",
                        mapping_file,
                        len(self._mapping),
                    )
                    return
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Failed to load identity mapping file %s: %s",
                    mapping_file,
                    exc,
                )

        # Priority 2: Inline JSON string
        mapping_str = os.getenv("IDENTITY_MAPPING")
        if mapping_str:
            try:
                self._mapping = json.loads(mapping_str)
                logger.info(
                    "Loaded identity mapping from IDENTITY_MAPPING env (%d entries)",
                    len(self._mapping),
                )
                return
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Failed to parse IDENTITY_MAPPING env: %s", exc,
                )

        logger.info("No identity mapping configured — using default viewer role")

    # ── Resolution ────────────────────────────────────────────────────

    def resolve(self, platform: str, platform_user_id: str) -> UserIdentity:
        """Resolve a platform user to a :class:`UserIdentity`.

        Unmapped users receive the ``"viewer"`` role with read-only
        access to all namespaces.

        Args:
            platform: Platform key (``"slack"``, ``"teams"``, etc.).
            platform_user_id: Platform-native user identifier.

        Returns:
            Resolved :class:`UserIdentity` with RBAC context.
        """
        key = f"{platform}:{platform_user_id}"
        user_data = self._mapping.get(key, {})

        return UserIdentity(
            platform=platform,
            platform_user_id=platform_user_id,
            internal_user_id=user_data.get("internal_id", platform_user_id),
            display_name=user_data.get("display_name", "Unknown User"),
            rbac_role=user_data.get("rbac_role", "viewer"),
            allowed_namespaces=user_data.get("namespaces", ["*"]),
            allowed_operations=user_data.get("operations", ["read"]),
        )

    # ── Utilities ─────────────────────────────────────────────────────

    def is_mapped(self, platform: str, platform_user_id: str) -> bool:
        """Check if a platform user has an explicit mapping."""
        return f"{platform}:{platform_user_id}" in self._mapping
