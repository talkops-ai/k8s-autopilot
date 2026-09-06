"""Middleware registry — singleton with ``@register_middleware`` decorator.

Ported from ``reference/opscode/src/opscode/middleware/registry.py``.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, TypeVar

from langchain.agents.middleware.types import AgentMiddleware

BaseAgentMiddleware = AgentMiddleware

T = TypeVar("T", bound=type)


class MiddlewareRegistry:
    """Registry of middleware providers. Preserves insertion order."""

    _instance: MiddlewareRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._registry: dict[str, tuple[type[AgentMiddleware], dict[str, Any]]] = {}

    def register(
        self,
        name: str,
        cls: type[AgentMiddleware],
        *,
        default_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Register a middleware class under *name*."""
        self._registry[name] = (cls, default_kwargs or {})

    def get(self, name: str) -> type[AgentMiddleware] | None:
        """Return the middleware class for *name*, or ``None``."""
        entry = self._registry.get(name)
        return entry[0] if entry else None

    def list_registered(self) -> list[str]:
        """Return sorted list of registered middleware names."""
        return sorted(self._registry.keys())

    def build_stack(
        self,
        *,
        exclude: set[str] | None = None,
        **kwargs: Any,
    ) -> list[AgentMiddleware]:
        """Build the full middleware stack, preserving insertion order."""
        items = [
            (name, item)
            for name, item in self._registry.items()
            if not (exclude and name in exclude)
        ]
        stack = []
        for name, (cls, default_kwargs) in items:
            inst_kwargs = {**default_kwargs, **kwargs.get(name, {})}
            stack.append(cls(**inst_kwargs))
        return stack

    def build_middleware(self, name: str, **kwargs: Any) -> AgentMiddleware:
        """Instantiate a single registered middleware by name.

        Merges *kwargs* with the registered default_kwargs.
        Raises ``KeyError`` if *name* is not registered.
        """
        entry = self._registry.get(name)
        if entry is None:
            raise KeyError(f"Middleware {name!r} not registered")
        cls, default_kwargs = entry
        merged = {**default_kwargs, **kwargs}
        return cls(**merged)

    @classmethod
    def get_instance(cls) -> MiddlewareRegistry:
        """Return the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def ensure_discovered(cls) -> MiddlewareRegistry:
        """Ensure all middleware modules are imported (firing @register_middleware).

        This is necessary when accessing the registry outside of agent/factory.py,
        e.g. in tests, since modules only register on first import.
        """
        inst = cls.get_instance()
        if inst._registry:
            return inst

        import importlib

        _modules = [
            "k8s_autopilot.middleware.ask_user",
            "k8s_autopilot.middleware.auto_mode",
            "k8s_autopilot.middleware.auto_mode_hitl",
            "k8s_autopilot.middleware.compaction",
            "k8s_autopilot.middleware.configurable_model",
            "k8s_autopilot.middleware.cost_tracking",
            "k8s_autopilot.middleware.glm_stall_recovery",
            "k8s_autopilot.middleware.goal_criteria",
            "k8s_autopilot.middleware.goal_tools",
            "k8s_autopilot.middleware.headless_mcp_guard",
            "k8s_autopilot.middleware.human_in_the_loop",
            "k8s_autopilot.middleware.local_context",
            "k8s_autopilot.middleware.memory_guard",
            "k8s_autopilot.middleware.reliable_rubric",
            "k8s_autopilot.middleware.resume_state",
            "k8s_autopilot.middleware.server_hooks",
            "k8s_autopilot.middleware.shell_allow_list",
            "k8s_autopilot.middleware.skill_shortcut",
            "k8s_autopilot.middleware.skills",
            "k8s_autopilot.middleware.subagents",
            "k8s_autopilot.middleware.tool_filter",
            "k8s_autopilot.middleware.unified_system_message",
            "k8s_autopilot.middleware.a2ui_buffer",
        ]
        for mod_name in _modules:
            try:
                importlib.import_module(mod_name)
            except ImportError:
                pass

        return inst


def get_middleware_registry() -> MiddlewareRegistry:
    """Module-level accessor for the singleton ``MiddlewareRegistry``.

    Ensures all middleware modules are imported on first access.
    """
    return MiddlewareRegistry.ensure_discovered()


def register_middleware(
    name: str,
    *,
    default_kwargs: dict[str, Any] | None = None,
) -> Callable[[T], T]:
    """Decorator to register a middleware class.

    Usage::

        @register_middleware(name="ask_user")
        class AskUserMiddleware(AgentMiddleware):
            ...
    """

    def decorator(cls: T) -> T:
        get_middleware_registry().register(name, cls, default_kwargs=default_kwargs)  # type: ignore[arg-type]
        return cls

    return decorator
