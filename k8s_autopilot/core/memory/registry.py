"""Dynamic Memory Registry and Discovery for k8s-autopilot.

Resolves memory directories and files (user preference profiles, project specs,
and operator-specific policies) and maps them to virtual filesystem paths.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional
import threading

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("MemoryRegistry")


def get_user_memory_path() -> Path:
    """Return the physical path of the user-level AGENTS.md file."""
    user_dir = Path.home() / ".agents"
    return user_dir / "AGENTS.md"


class MemoryRegistry:
    """Registry that discovers and manages memory files across user/project/domain scopes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._project_root: Optional[Path] = None
        self._initialized_user = False

    def get_project_root(self) -> Path:
        """Resolve project root directory."""
        if self._project_root is None:
            from k8s_autopilot.core.backend import get_project_root
            self._project_root = get_project_root()
        return self._project_root

    def _ensure_user_memory_initialized(self) -> Path:
        """Ensure ~/.agents/AGENTS.md exists, creating it if missing."""
        user_file = get_user_memory_path()
        if not self._initialized_user:
            with self._lock:
                if not self._initialized_user:
                    if not user_file.exists():
                        try:
                            user_file.parent.mkdir(parents=True, exist_ok=True)
                            default_content = (
                                "# User Preferences\n\n"
                                "<!-- deepagents:onboarding-name:start -->\n"
                                "<!-- deepagents:onboarding-name:end -->\n"
                            )
                            user_file.write_text(default_content, encoding="utf-8")
                            logger.info(f"Initialized default user AGENTS.md at {user_file}")
                        except OSError as e:
                            logger.warning(f"Could not initialize user AGENTS.md: {e}")
                    self._initialized_user = True
        return user_file

    def get_memory_paths_for_domain(self, domain: str) -> List[str]:
        """Resolve and return virtual memory paths for a given domain/scope.

        Supports special scopes: 'user', 'project', 'global', and operator domains.
        """
        paths: List[str] = []

        if domain == "user":
            user_file = self._ensure_user_memory_initialized()
            if user_file.exists():
                paths.append("/memories/user/AGENTS.md")

        elif domain == "project":
            root = self.get_project_root()
            # Check project-specific memory directories
            p_custom = root / ".agents" / "AGENTS.md"
            p_root = root / "AGENTS.md"
            if p_custom.exists():
                paths.append("/memories/project/.agents/AGENTS.md")
            if p_root.exists():
                paths.append("/memories/project/AGENTS.md")

        elif domain == "global":
            # Any global/shared policies that apply to all operators
            shared_memory_dir = self.get_project_root() / "memory" / "global"
            if shared_memory_dir.exists() and shared_memory_dir.is_dir():
                for path in shared_memory_dir.rglob("*.md"):
                    if path.is_file() and not path.name.startswith("."):
                        try:
                            rel = path.relative_to(shared_memory_dir).as_posix()
                            paths.append(f"/memories/global/{rel}")
                        except ValueError:
                            pass
        else:
            # Domain-specific operator memories (e.g. helm-operator)
            # Only discover top-level .md files; role-specific subdirectories
            # (e.g. coordinator/, helm-operation/) are added explicitly per-agent.
            domain_dir = self.get_project_root() / "plugins" / domain
            if domain_dir.exists() and domain_dir.is_dir():
                for path in domain_dir.glob("*.md"):
                    if path.is_file() and not path.name.startswith("."):
                        try:
                            rel = path.relative_to(domain_dir).as_posix()
                            paths.append(f"/memories/{domain}/{rel}")
                        except ValueError:
                            pass

        return sorted(paths)

    def get_memory_paths_for_domain_and_role(self, domain: str, role: Optional[str] = None) -> List[str]:
        """Resolve and return virtual memory paths for a given domain and optional role.

        If a role-specific AGENTS.md exists (e.g., plugins/{domain}/agents/{role}/AGENTS.md):
        - Adds "/memories/{domain}/{role}/AGENTS.md"
        - Excludes the domain-level "/memories/{domain}/AGENTS.md"
        """
        paths: List[str] = []
        root = self.get_project_root()

        # 1. Check if role-specific AGENTS.md exists
        has_role_agents = False
        if role:
            role_agents = root / "plugins" / domain / "agents" / role / "AGENTS.md"
            if role_agents.is_file():
                paths.append(f"/memories/{domain}/{role}/AGENTS.md")
                has_role_agents = True

        # 2. Get top-level domain memory files
        domain_dir = root / "plugins" / domain
        if domain_dir.exists() and domain_dir.is_dir():
            for path in domain_dir.glob("*.md"):
                if path.is_file() and not path.name.startswith("."):
                    if path.name == "AGENTS.md" and has_role_agents:
                        continue
                    try:
                        rel = path.relative_to(domain_dir).as_posix()
                        paths.append(f"/memories/{domain}/{rel}")
                    except ValueError:
                        pass

        return sorted(paths)

    def resolve_virtual_path(self, virtual_path: str) -> Path:
        """Resolve a virtual memory path starting with /memories/ to a physical Path."""
        root = self.get_project_root()
        if virtual_path.startswith("/memories/user/"):
            rel = virtual_path[len("/memories/user/"):]
            return Path.home() / ".agents" / rel
        elif virtual_path.startswith("/memories/project/"):
            rel = virtual_path[len("/memories/project/"):]
            return root / rel
        elif virtual_path.startswith("/memories/global/"):
            rel = virtual_path[len("/memories/global/"):]
            return root / "memory" / "global" / rel
        elif virtual_path.startswith("/memories/"):
            # Format: /memories/<some-domain>/<relative-path>
            parts = [p for p in virtual_path.strip("/").split("/") if p]
            if len(parts) >= 2:
                domain_part = parts[1]
                remaining = parts[2:]
                if len(remaining) == 1:
                    # e.g., /memories/helm-operator/AGENTS.md
                    return root / "plugins" / domain_part / remaining[0]
                elif len(remaining) > 1:
                    # e.g., /memories/helm-operator/helm-operation/AGENTS.md
                    return root / "plugins" / domain_part / "agents" / remaining[0] / "/".join(remaining[1:])
        return Path(virtual_path)


# ── Global Singleton and lookup helper ─────────────────────────────────────

_registry_instance: Optional[MemoryRegistry] = None
_registry_lock = threading.Lock()


def get_memory_registry() -> MemoryRegistry:
    """Return the global MemoryRegistry singleton."""
    global _registry_instance
    if _registry_instance is None:
        with _registry_lock:
            if _registry_instance is None:
                _registry_instance = MemoryRegistry()
    return _registry_instance
