"""Singleton registry for backend providers."""

from __future__ import annotations

import threading
from typing import Any


class BackendRegistry:
    """Singleton registry for backend providers."""

    _instance: BackendRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._providers: dict[str, type] = {}

    def register(self, name: str, cls: type) -> None:
        """Register a backend provider class under *name*."""
        self._providers[name] = cls

    def build(self, name: str, **kwargs: Any) -> Any:
        """Instantiate a registered backend by *name*."""
        cls = self._providers.get(name)
        if cls is None:
            raise ValueError(f"Backend provider '{name}' is not registered.")
        return cls(**kwargs)

    def list_registered(self) -> list[str]:
        """Return sorted list of registered backend names."""
        return sorted(self._providers.keys())

    @classmethod
    def get_instance(cls) -> BackendRegistry:
        """Return the singleton instance, creating it if needed."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance


def get_backend_registry() -> BackendRegistry:
    """Module-level accessor for the singleton ``BackendRegistry``."""
    return BackendRegistry.get_instance()


def register_backend(name: str):  # type: ignore[no-untyped-def]
    """Class decorator to register a backend provider.

    Usage::

        @register_backend("local")
        class LocalShellBackend(SDKLocalShellBackend):
            ...
    """

    def decorator(cls: type) -> type:
        get_backend_registry().register(name, cls)
        return cls

    return decorator
