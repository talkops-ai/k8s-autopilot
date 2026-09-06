"""Composite backend routing for K8s Autopilot.

Routes special virtual paths (``/large_tool_results/``) to temporary
backends while delegating everything else to the default ``LocalShellBackend``.

Ported from ``reference/opscode/src/opscode/backend/composite.py``.
"""

from __future__ import annotations

import tempfile
from typing import Any

from deepagents.backends import BackendProtocol, CompositeBackend, FilesystemBackend


class K8sCompositeBackend(CompositeBackend):
    """Composite backend routing special paths to virtual backends."""

    def __init__(
        self,
        default: Any,
        routes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        import atexit
        from k8s_autopilot.config.paths import ensure_conversation_history_dir

        self._large_results_dir = tempfile.mkdtemp(prefix="k8s_autopilot_large_results_")
        self._conversation_history_dir = ensure_conversation_history_dir()

        atexit.register(self.cleanup)

        large_results_backend = FilesystemBackend(
            root_dir=self._large_results_dir,
            virtual_mode=True,
        )
        conversation_history_backend = FilesystemBackend(
            root_dir=self._conversation_history_dir,
            virtual_mode=True,
        )

        effective_routes: dict[str, BackendProtocol] = {
            "/large_tool_results/": large_results_backend,
            "/conversation_history/": conversation_history_backend,
        }
        if routes:
            effective_routes.update(routes)

        super().__init__(default=default, routes=effective_routes, **kwargs)

    def cleanup(self) -> None:
        """Clean up temporary directories (leave persistent conversation history intact)."""
        import shutil

        if hasattr(self, "_large_results_dir") and self._large_results_dir:
            shutil.rmtree(self._large_results_dir, ignore_errors=True)

    def __enter__(self) -> K8sCompositeBackend:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()
