import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from k8s_autopilot.utils.logger import AgentLogger

from deepagents.backends import FilesystemBackend

logger = AgentLogger("K8sBackend")


class SkillsFilesystemBackend(FilesystemBackend):
    """Custom FilesystemBackend for /skills/ route.

    Enforces virtual_mode prefix checks but allows symlinks that resolve to
    the 'plugins/' directory inside the project root, enabling unified plugin
    skills structure support without breaking isolation.
    """

    def _resolve_path(self, key: str) -> Path:
        if self.virtual_mode:
            vpath = key if key.startswith("/") else "/" + key
            if ".." in vpath or vpath.startswith("~"):
                raise ValueError("Path traversal not allowed")

            from k8s_autopilot.core.backend import get_project_root
            project_root = get_project_root()
            plugins_dir = project_root / "plugins"

            # Allow absolute physical paths directly if they fall under allowed roots
            try:
                key_path = Path(key)
                if key_path.is_absolute():
                    full = key_path.resolve()
                    if full.is_relative_to(plugins_dir) or full.is_relative_to(self.cwd):
                        return full
            except Exception:
                pass

            # Map virtual path parts back to physical plugins/ directory
            parts = [p for p in vpath.split("/") if p]
            if len(parts) >= 1:
                domain = parts[0]
                if len(parts) == 1:
                    # e.g. /skills/helm-operator -> plugins/helm-operator/skills
                    return (plugins_dir / domain / "skills").resolve()

                # Traverse parts[1:] to check if they correspond to nested agent directories
                curr_dir = plugins_dir / domain
                consumed_idx = 1
                for part in parts[1:]:
                    next_agent_dir = curr_dir / "agents" / part
                    if next_agent_dir.exists() and next_agent_dir.is_dir():
                        curr_dir = next_agent_dir
                        consumed_idx += 1
                    else:
                        break

                if curr_dir != plugins_dir / domain:
                    subpath = Path(*parts[consumed_idx:]) if len(parts) > consumed_idx else Path("")
                    return (curr_dir / "skills" / subpath).resolve()
                else:
                    # Coordinator/domain skill path: plugins/{domain}/skills/{subpath}
                    subpath = Path(*parts[1:])
                    return (plugins_dir / domain / "skills" / subpath).resolve()

            full = (self.cwd / vpath.lstrip("/")).resolve()
            try:
                full.relative_to(self.cwd)
                return full
            except ValueError:
                pass

            try:
                full.relative_to(plugins_dir)
                return full
            except ValueError:
                msg = f"Path:{full} outside allowed root directories: {self.cwd} and {plugins_dir}"
                raise ValueError(msg) from None

        return super()._resolve_path(key)

    def _to_virtual_path(self, path: Path) -> str:
        if self.virtual_mode:
            resolved = path.resolve()
            try:
                rel = resolved.relative_to(self.cwd)
                return "/" + rel.as_posix()
            except ValueError:
                pass

            # Map from plugins/ back to skills/ subpath (relative to self.cwd)
            from k8s_autopilot.core.backend import get_project_root
            project_root = get_project_root()
            plugins_dir = project_root / "plugins"
            try:
                rel_to_plugins = resolved.relative_to(plugins_dir)
                parts = rel_to_plugins.parts
                if "skills" in parts:
                    skills_idx = parts.index("skills")
                    before_skills = parts[:skills_idx]
                    filtered = [p for p in before_skills if p != "agents"]
                    after_skills = parts[skills_idx + 1:]
                    return "/" + Path(*(filtered + list(after_skills))).as_posix()
            except ValueError:
                pass

        return super()._to_virtual_path(path)

def get_config() -> Any:
    """Retrieve resolved Config from the central configuration engine."""
    from k8s_autopilot.config.config import Config
    return Config()

def get_project_root() -> Path:
    """Filesystem root for virtual paths (``/workspace/...``, etc.)."""
    cfg = get_config()
    raw = cfg.AGENT_PROJECT_ROOT or ""
    raw = raw.strip()
    return Path(raw).resolve() if raw else Path.cwd().resolve()

def _helm_workspace_dir() -> Path:
    """Physical directory for generated charts (virtual ``/workspace/helm-charts/...``)."""
    root = get_project_root()
    cfg = get_config()
    rel = cfg.HELM_WORKSPACE or "workspace/helm-charts"
    p = Path(rel)
    return p.resolve() if p.is_absolute() else (root / p).resolve()

def sync_workspace_to_disk(
    files: Dict[str, Any],
    *,
    prefix: str = "/workspace/",
) -> Dict[str, Any]:
    """
    Materialise files from the coordinator's virtual ``StateBackend`` run state
    onto the actual physical filesystem (e.g. before launching helm tools or git actions).
    """
    root = get_project_root()
    helm_base = _helm_workspace_dir()
    written: Dict[str, Any] = {}

    for vpath, payload in files.items():
        if not vpath.startswith(prefix):
            continue

        rel_path = vpath[len(prefix):]
        # Map virtual workspace/helm-charts/... to physical helm-charts
        if rel_path.startswith("helm-charts/"):
            chart_rel = rel_path[len("helm-charts/"):]
            physical_path = helm_base / chart_rel
        else:
            physical_path = root / rel_path

        try:
            physical_path.parent.mkdir(parents=True, exist_ok=True)
            content = payload.get("content", "")
            if isinstance(content, str):
                physical_path.write_text(content, encoding="utf-8")
            else:
                physical_path.write_bytes(content)
            written[vpath] = content
        except Exception as e:
            logger.error(f"Failed to write file to disk during sync: {physical_path} ({e})")

    if written:
        logger.info(
            f"sync_workspace_to_disk: materialised virtual files (helm_base: {helm_base}, root: {root})"
        )
    else:
        logger.debug(
            f"sync_workspace_to_disk: no /workspace/ files found in state (prefix: {prefix})"
        )

    return written

def get_memories_namespace(_rt: Any = None) -> tuple[str, ...]:
    """Retrieve a thread-scoped memories namespace tuple.
    
    Checks the active runnable configuration's thread ID and returns
    (org, thread_id) if present; otherwise falls back to (org,).
    """
    from langgraph.config import get_config as lg_get_config
    cfg = get_config()
    _org = cfg.ORG_NAME or "default_org"
    try:
        lg_cfg = lg_get_config()
        tid = lg_cfg.get("configurable", {}).get("thread_id")
        if tid:
            return (_org, tid)
    except Exception:  # noqa: BLE001
        pass
    return (_org,)

class K8sBackendMixin:
    """
    Mixin that supplies ``make_backend()`` and ``seed_files()`` for
    all K8s Autopilot deep agents.

    Routing:
        ``/memories/``  → ``StoreBackend`` (cross-thread persistence via LangGraph Store)
        ``/shared/``    → ``StoreBackend`` (cross-domain scratchpad — shared across coordinators)
        ``/skills/``    → ``StateBackend`` (ephemeral — planner-generated skill files)
        ``/workspace/`` → ``StateBackend`` (ephemeral — generated Helm chart files)
        default         → remote sandbox if configured, falling back to local shell execution
    """

    @staticmethod
    def make_backend() -> Any:  # CompositeBackend
        """Build a ``CompositeBackend`` for the deep agent's virtual filesystem.

        Since ``deepagents>=0.5.0``, backends resolve runtime context
        internally via LangGraph's ``get_config()`` / ``get_store()`` /
        ``get_runtime()`` — no need to pass ``runtime`` explicitly.
        """
        from deepagents.backends import (
            CompositeBackend,
            StateBackend,
            StoreBackend,
        )

        root = get_project_root()
        helm_base = _helm_workspace_dir()
        helm_base.mkdir(parents=True, exist_ok=True)

        from k8s_autopilot.core.sandbox import get_sandbox_backend
        from langgraph.config import get_config as lg_get_config

        try:
            lg_cfg = lg_get_config()
            thread_id = lg_cfg.get("configurable", {}).get("thread_id")
        except Exception:
            thread_id = None

        default = get_sandbox_backend(thread_id=thread_id)

        from deepagents.backends import FilesystemBackend
        from pathlib import Path

        from deepagents.backends.protocol import BackendProtocol
        routes: Dict[str, BackendProtocol] = {
            "/memories/user/": FilesystemBackend(
                root_dir=str(Path.home() / ".agents"),
                virtual_mode=True,
            ),
            "/memories/project/": FilesystemBackend(
                root_dir=str(root),
                virtual_mode=True,
            ),
            "/memories/helm-operator/": FilesystemBackend(
                root_dir=str(root / "memory" / "helm-operator"),
                virtual_mode=True,
            ),
            "/memories/observability/": FilesystemBackend(
                root_dir=str(root / "memory" / "observability"),
                virtual_mode=True,
            ),
            "/memories/k8s-operator/": FilesystemBackend(
                root_dir=str(root / "memory" / "k8s-operator"),
                virtual_mode=True,
            ),
            "/memories/app-operator/": FilesystemBackend(
                root_dir=str(root / "memory" / "app-operator"),
                virtual_mode=True,
            ),
            "/memories/": StoreBackend(
                namespace=get_memories_namespace,
            ),
            "/shared/": StoreBackend(
                namespace=lambda _rt: ("shared",),
            ),
            "/skills/": SkillsFilesystemBackend(
                root_dir=str(root / "skills"),
                virtual_mode=True,
            ),
            # ── Conversation history offloading (dcode pattern) ───────────
            # ConversationCompactionMiddleware writes full transcripts here
            # when token budget is exceeded. Agents can read_file() to
            # recall past context on demand.
            "/conversation_history/": FilesystemBackend(
                root_dir=str(root / ".conversation_history"),
                virtual_mode=True,
            ),
        }

        return CompositeBackend(
            default=default,
            routes=routes,
        )

    @staticmethod
    def seed_files(
        skill_paths: Optional[List[str]] = None,
        memory_paths: Optional[List[str]] = None,
        project_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Load explicitly requested skill files and initial memory into the virtual FS dict.
        """
        from deepagents.backends.utils import create_file_data

        root = project_root or get_project_root()
        files: Dict[str, Any] = {}

        if skill_paths:
            # First, try to seed via the dynamic SkillRegistry
            try:
                from k8s_autopilot.core.skills.registry import get_skill_registry
                registry_files = get_skill_registry().seed_skills_files(skill_paths)
                files.update(registry_files)
            except Exception as e:
                from k8s_autopilot.utils.logger import AgentLogger
                AgentLogger("K8sBackendMixin").warning(f"Failed to seed skills via registry: {e}")

            # Fallback loading for any paths not already populated by the registry
            skills_dir = root / "skills"
            for vpath in skill_paths:
                # Skip if already successfully seeded by the registry
                if any(k.startswith(vpath) for k in files):
                    continue

                rel = vpath.removeprefix("/skills/").lstrip("/")
                disk_path = skills_dir / rel

                # If it's a directory, look for SKILL.md
                if vpath.endswith("/"):
                    skill_file = disk_path / "SKILL.md"
                    if skill_file.exists():
                        target_vpath = vpath + "SKILL.md"
                        try:
                            files[target_vpath] = create_file_data(
                                skill_file.read_text(encoding="utf-8")
                            )
                        except Exception as e:
                            logger.error(f"Failed to seed skill file {skill_file}: {e}")
                else:
                    if disk_path.exists():
                        try:
                            files[vpath] = create_file_data(
                                disk_path.read_text(encoding="utf-8")
                            )
                        except Exception as e:
                            logger.error(f"Failed to seed skill file {disk_path}: {e}")

        if memory_paths:
            for vpath in memory_paths:
                # Skip physical memories (their directories are handled directly by routed FilesystemBackend)
                if vpath.startswith(("/memories/user/", "/memories/project/", "/memories/helm-operator/", "/memories/observability/", "/memories/k8s-operator/", "/memories/app-operator/")):
                    continue

                # Virtual path formatting: /memories/{thread_id}/AGENTS.md
                parts = [p for p in vpath.split("/") if p]
                if len(parts) >= 2 and parts[-1] == "AGENTS.md":
                    # Load default user onboarding preferences
                    try:
                        from k8s_autopilot.core.memory.registry import get_user_memory_path
                        user_agents_path = get_user_memory_path()
                        if user_agents_path.exists():
                            files[vpath] = create_file_data(
                                user_agents_path.read_text(encoding="utf-8")
                            )
                    except Exception as e:
                        logger.error(f"Failed to seed default onboarding memory for route {vpath}: {e}")

        return files
