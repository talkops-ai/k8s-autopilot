"""Backend module for K8s Autopilot — shell execution and filesystem access."""

from typing import Any

from k8s_autopilot.backend.registry import (
    BackendRegistry,
    get_backend_registry,
    register_backend,
)

__all__ = [
    "BackendRegistry",
    "get_backend_registry",
    "register_backend",
]


def __getattr__(name: str) -> Any:
    """Lazy-load backend classes to avoid circular imports."""
    _lazy_map = {
        "LocalShellBackend": "k8s_autopilot.backend.local",
        "K8sCompositeBackend": "k8s_autopilot.backend.composite",
    }
    if name in _lazy_map:
        import importlib

        mod = importlib.import_module(_lazy_map[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
