"""
K8s Autopilot — Shared Backend factory and file seeding for deep agents.

Routing:
    /memories/  → StoreBackend  (cross-thread persistence via LangGraph Store)
    /shared/    → StoreBackend  (cross-domain scratchpad — shared across coordinators)
    /skills/    → StateBackend  (ephemeral — planner-generated skill files)
    /workspace/ → StateBackend  (ephemeral — generated Helm chart files)
    default     → LocalShellBackend (real filesystem + shell for helm/kubectl CLI)

The ``/workspace/`` route is intentionally virtual (StateBackend) so that
``write_file`` calls always succeed without needing real directory creation.
Before execution against the disk, the coordinator must call ``sync_workspace_to_disk()``
to materialise the virtual files onto the real filesystem.

The ``/shared/`` route uses a global ``InMemoryStore`` instance so that
all coordinators can read and write cross-domain findings.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("K8sBackend")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_project_root() -> Path:
    """Filesystem root for virtual paths (``/workspace/...``, etc.)."""
    raw = os.getenv("AGENT_PROJECT_ROOT", "").strip()
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def _helm_workspace_dir() -> Path:
    """Physical directory for generated charts (virtual ``/workspace/helm-charts/...``)."""
    root = get_project_root()
    rel = os.getenv("HELM_WORKSPACE", "workspace/helm-charts")
    p = Path(rel)
    return p.resolve() if p.is_absolute() else (root / p).resolve()


# ---------------------------------------------------------------------------
# Workspace sync: virtual → real filesystem
# ---------------------------------------------------------------------------

def sync_workspace_to_disk(
    files: Dict[str, Any],
    *,
    prefix: str = "/workspace/",
    project_root: Optional[Path] = None,
) -> Dict[str, Path]:
    """Materialise virtual ``/workspace/`` files from state onto real disk."""
    root = project_root or get_project_root()
    written: Dict[str, Path] = {}

    for vpath, file_data in files.items():
        if not vpath.startswith(prefix):
            continue

        if isinstance(file_data, dict):
            content = file_data.get("content", "")
        elif isinstance(file_data, str):
            content = file_data
        else:
            content = getattr(file_data, "content", str(file_data))

        rel = vpath.lstrip("/")
        real_path = root / rel

        real_path.parent.mkdir(parents=True, exist_ok=True)
        real_path.write_text(content, encoding="utf-8")
        written[vpath] = real_path

    if written:
        logger.info(
            "sync_workspace_to_disk: materialised virtual files",
            extra={
                "file_count": len(written),
                "paths": list(written.keys())[:10],
            },
        )
    else:
        logger.warning(
            "sync_workspace_to_disk: no /workspace/ files found in state",
            extra={"total_files_in_state": len(files)},
        )

    return written


# ---------------------------------------------------------------------------
# Backend factory mixin
# ---------------------------------------------------------------------------

class K8sBackendMixin:
    """
    Mixin that supplies ``make_backend()`` and ``seed_files()`` for
    all K8s Autopilot deep agents.

    Routing:
        ``/memories/``  → ``StoreBackend`` (cross-thread persistence)
        ``/shared/``    → ``StoreBackend`` (cross-domain scratchpad)
        ``/skills/``    → ``StateBackend`` (ephemeral — skills)
        ``/workspace/`` → ``StateBackend`` (ephemeral — generated chart files)
        default         → ``LocalShellBackend`` (virtual FS under project root
                          + ``execute`` for helm/kubectl CLI commands)
    """

    @staticmethod
    def make_backend(runtime: Any) -> Any:  # CompositeBackend
        from deepagents.backends import (
            CompositeBackend,
            LocalShellBackend,
            StateBackend,
            StoreBackend,
        )

        root = get_project_root()
        helm_base = _helm_workspace_dir()
        helm_base.mkdir(parents=True, exist_ok=True)

        shell_env = {
            "HELM_EXPERIMENTAL_OCI": "1",
        }

        default = LocalShellBackend(
            root_dir=str(root),
            virtual_mode=True,
            env=shell_env,
            inherit_env=True,
        )

        return CompositeBackend(
            default=default,
            routes={
                "/memories/": StoreBackend(
                    runtime,
                    namespace=lambda ctx: (
                        ctx.context.get("org_name", "default_org")
                        if isinstance(ctx.context, dict)
                        else getattr(ctx.context, "org_name", "default_org"),
                    ),
                ),
                "/shared/": StoreBackend(
                    runtime,
                    namespace=lambda _ctx: ("shared",),
                ),
                "/skills/": StateBackend(runtime),
            },
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
            skills_dir = root / "skills"
            for vpath in skill_paths:
                vpath_cleaned = vpath.strip("/")
                if not vpath_cleaned.startswith("skills"):
                    continue
                rel_path = vpath_cleaned[len("skills/"):]
                target_path = skills_dir / rel_path
                
                if target_path.is_file() and not target_path.name.startswith("."):
                    try:
                        files[vpath] = create_file_data(target_path.read_text(encoding="utf-8"))
                    except UnicodeDecodeError:
                        pass
                elif target_path.is_dir():
                    for path in target_path.rglob("*"):
                        if path.is_file() and not path.name.startswith("."):
                            try:
                                child_vpath = f"/skills/{path.relative_to(skills_dir).as_posix()}"
                                files[child_vpath] = create_file_data(path.read_text(encoding="utf-8"))
                            except UnicodeDecodeError:
                                pass

        if memory_paths:
            memory_dir = root / "memory"
            for vpath in memory_paths:
                vpath_cleaned = vpath.strip("/")
                if not vpath_cleaned.startswith("memories"):
                    continue
                rel_path = vpath_cleaned[len("memories/"):]
                target_path = memory_dir / rel_path

                if target_path.is_file() and not target_path.name.startswith("."):
                    try:
                        files[vpath] = create_file_data(target_path.read_text(encoding="utf-8"))
                    except UnicodeDecodeError:
                        pass
                elif target_path.is_dir():
                    for path in target_path.rglob("*"):
                        if path.is_file() and not path.name.startswith("."):
                            try:
                                child_vpath = f"/memories/{path.relative_to(memory_dir).as_posix()}"
                                files[child_vpath] = create_file_data(path.read_text(encoding="utf-8"))
                            except UnicodeDecodeError:
                                pass

        logger.info(
            "seed_files: selectively loaded from disk",
            extra={
                "skills_count": sum(1 for k in files if k.startswith("/skills/")),
                "memory_count": sum(1 for k in files if k.startswith("/memories/")),
            },
        )

        return files
