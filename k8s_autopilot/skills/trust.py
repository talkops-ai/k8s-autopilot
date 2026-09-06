"""Trust store for skill directories to prevent unauthorized execution of untrusted scripts.

Ported from ``reference/opscode/src/opscode/skills/trust.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from k8s_autopilot.config import paths

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class SkillTrustStore:
    """Tracks approved skill directories to prevent remote execution exploits."""

    def __init__(self, trust_file_path: Path | None = None) -> None:
        if trust_file_path is None:
            self.trust_file_path = paths.DATA_DIR / "skill_trust.json"
        else:
            self.trust_file_path = trust_file_path
        self._trusted_dirs: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.trust_file_path.exists():
            return
        try:
            data = json.loads(self.trust_file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "dirs" in data:
                self._trusted_dirs = set(data["dirs"])
            elif isinstance(data, list):
                self._trusted_dirs = set(data)
        except Exception as e:
            logger.warning("Could not read skill trust store: %s", e)

    def _save(self) -> None:
        try:
            self.trust_file_path.parent.mkdir(parents=True, exist_ok=True)
            self.trust_file_path.write_text(
                json.dumps({"version": 1, "dirs": list(self._trusted_dirs)}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save skill trust store: %s", e)

    def compute_hash(self, skill_path: Path) -> str:
        """Compute a hash of the skill path directory name (for matching/bookkeeping)."""
        resolved = str(skill_path.expanduser().resolve())
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()

    def is_trusted(self, name: str, skill_path: Path) -> bool:
        """Check if a skill path is trusted. Built-in and user home skills are auto-trusted."""
        try:
            resolved = skill_path.expanduser().resolve()
        except Exception:
            return False

        # Auto-trust built-in skills
        built_in_dir = Path(__file__).parent.parent / "built_in_skills"
        try:
            if resolved.is_relative_to(built_in_dir.resolve()):
                return True
        except Exception:
            pass

        # Auto-trust user home skills
        user_sd = paths.user_skills_dir("k8s-autopilot")
        try:
            if resolved.is_relative_to(user_sd.resolve()):
                return True
        except Exception:
            pass

        return str(resolved) in self._trusted_dirs

    def trust_skill(self, name: str, skill_path: Path) -> None:
        """Add a skill path to the trust store."""
        try:
            resolved = skill_path.expanduser().resolve()
            self._trusted_dirs.add(str(resolved))
            self._save()
        except Exception as e:
            logger.warning("Could not trust skill path: %s", e)
