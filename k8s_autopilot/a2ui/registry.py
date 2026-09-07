"""A2UI Component Registry.

Provides a pluggable component registry for A2UI rendering. Components are
self-contained classes that know how to:
  1. Decide whether they can handle a given render context (can_handle)
  2. Build the A2UI JSON messages for that context (build)

Usage:
    # Register a custom component
    @register_component(priority=20)
    class MyCustomComponent(BaseComponent):
        component_type = "my_custom"

        def can_handle(self, ctx): ...
        def build(self, ctx): ...

    # Use the registry
    registry = get_registry()
    parts = registry.build_parts(context)
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from a2a.types import Part

try:
    from a2ui.a2a import create_a2ui_part
except ImportError:

    def create_a2ui_part(a2ui_data: dict) -> Part:  # type: ignore[misc]
        """Create an A2A Part containing serialized A2UI payload data.

        Args:
            a2ui_data: Dictionary representing the A2UI widget hierarchy.

        Returns:
            Part: A2A Part configured with application/json+a2ui mimeType.
        """
        from google.protobuf import (  # type: ignore[import-untyped]
            json_format,
            struct_pb2,
        )

        val_cls = getattr(struct_pb2, "Value", dict)
        parsed = json_format.ParseDict(a2ui_data, val_cls()) if hasattr(json_format, "ParseDict") else a2ui_data
        return Part(  # type: ignore[call-arg]
            data=parsed,
            metadata={"mimeType": "application/json+a2ui"},
        )


from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("A2UIRegistry")


# ---------------------------------------------------------------------------
# Render Context — the single input every component receives
# ---------------------------------------------------------------------------


@dataclass
class RenderContext:
    """Bundles all information needed to decide which component renders a response."""

    content: Any = ""
    status: str = "working"
    is_task_complete: bool = False
    require_user_input: bool = False
    response_type: str = "text"
    metadata: dict[str, Any] = field(default_factory=dict)
    # K8s-specific extensions
    phase_override: str | None = None
    agent_name: str | None = None
    use_ui: bool = False
    session_id: str | None = None
    task_id: str | None = None

    # ---------- convenience helpers ----------

    @property
    def content_str(self) -> str:
        """Return *content* as a display-friendly string."""
        if isinstance(self.content, dict):
            parts = []
            if self.content.get("summary"):
                parts.append(str(self.content["summary"]).strip())
            if self.content.get("message"):
                parts.append(str(self.content["message"]).strip())
            if self.content.get("question"):
                parts.append(str(self.content["question"]).strip())

            if parts:
                return "\n\n".join(parts)
            return str(self.content)
        return str(self.content) if self.content else "Processing..."

    @property
    def phase(self) -> str:
        """Extract the workflow phase from metadata or content."""
        if self.phase_override:
            return self.phase_override
        if isinstance(self.content, dict):
            return str(
                self.content.get("phase") or self.content.get("active_phase") or self.metadata.get("phase", "unknown")
            )
        return str(self.metadata.get("phase", "unknown"))


# ---------------------------------------------------------------------------
# Base Component
# ---------------------------------------------------------------------------


class BaseComponent(ABC):
    """Abstract base class for every A2UI component.

    Subclasses MUST set ``component_type`` to a unique identifier.
    Optionally set ``catalog_id`` to tie the component to a custom catalog.
    """

    component_type: str = ""
    catalog_id: str | None = None

    @abstractmethod
    def can_handle(self, ctx: RenderContext) -> bool:
        """Return True if this component should render the given context."""
        ...

    @abstractmethod
    def build(self, ctx: RenderContext) -> list[dict]:
        """Build and return a list of A2UI message dicts (beginRendering, surfaceUpdate, etc.)."""
        ...

    def build_parts(self, ctx: RenderContext) -> list[Part]:
        """Build A2UI messages and convert each to an A2A ``Part``."""
        return [create_a2ui_part(msg) for msg in self.build(ctx)]


# ---------------------------------------------------------------------------
# Component Registry
# ---------------------------------------------------------------------------


class ComponentRegistry:
    """Central registry for A2UI components.

    Components are tried in **descending priority** order.  The first
    component whose ``can_handle`` returns True renders the response.
    """

    def __init__(self) -> None:
        """Initialize ComponentRegistry with empty component and type indexes."""
        # (priority, component) — higher priority = checked first
        self._components: list[tuple[int, BaseComponent]] = []
        self._type_index: dict[str, BaseComponent] = {}

    # ---- registration ----------------------------------------------------

    def register(
        self,
        component: BaseComponent,
        priority: int = 0,
    ) -> None:
        """Add a component to the registry."""
        if not component.component_type:
            raise ValueError(
                f"{component.__class__.__name__} must define a non-empty 'component_type' class attribute."
            )
        self._components.append((priority, component))
        self._components.sort(key=lambda t: t[0], reverse=True)
        self._type_index[component.component_type] = component
        logger.debug(f"Registered component {component.component_type} (priority={priority})")

    # ---- dispatch --------------------------------------------------------

    def build_parts(self, ctx: RenderContext) -> list[Part]:
        """Find the first matching component and build A2A Parts.

        Falls back to a plain ``TextPart`` when no component matched.
        """
        for _priority, component in self._components:
            if component.can_handle(ctx):
                logger.debug(f"Dispatching to component {component.component_type}")
                return component.build_parts(ctx)

        # Fallback — no component matched
        logger.warning(f"No component matched for context (status={ctx.status}, response_type={ctx.response_type})")
        return [Part(text=ctx.content_str)]

    # ---- introspection ---------------------------------------------------

    def get_component(self, component_type: str) -> BaseComponent | None:
        """Retrieve a registered component by its type string.

        Args:
            component_type: The component type identifier.

        Returns:
            Optional[BaseComponent]: The component instance or None.
        """
        return self._type_index.get(component_type)

    def list_components(self) -> list[str]:
        """List all registered component type names in priority order.

        Returns:
            List[str]: Component type identifiers ordered by priority.
        """
        return [comp.component_type for _p, comp in self._components]

    def list_components_detailed(self) -> list[dict[str, Any]]:
        """Return detailed metadata for all registered components.

        Returns:
            List[Dict[str, Any]]: List of dictionaries containing type, class, catalog_id, and priority.
        """
        return [
            {
                "type": comp.component_type,
                "class": comp.__class__.__name__,
                "catalog_id": comp.catalog_id,
                "priority": pri,
            }
            for pri, comp in self._components
        ]


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_registry: ComponentRegistry | None = None


def get_registry() -> ComponentRegistry:
    """Return (or create) the global ``ComponentRegistry`` singleton."""
    global _registry
    if _registry is None:
        _registry = ComponentRegistry()
        # Auto-import standard components so they self-register
        _auto_discover_components()
    return _registry


def _auto_discover_components() -> None:
    """Auto-discover any declarative components if present."""
    pass


# ---------------------------------------------------------------------------
# Decorator for declarative registration
# ---------------------------------------------------------------------------


def register_component(
    priority: int = 0,
) -> Callable[[type[BaseComponent]], type[BaseComponent]]:
    """Class decorator that instantiates and registers a ``BaseComponent``."""

    def decorator(cls: type[BaseComponent]) -> type[BaseComponent]:
        """Instantiate and register the component class.

        Args:
            cls: The component class to instantiate and register.

        Returns:
            Type[BaseComponent]: The registered component class.
        """
        instance = cls()
        get_registry().register(instance, priority=priority)
        return cls

    return decorator
