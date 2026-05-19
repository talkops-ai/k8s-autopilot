"""
A2UI Component Registry

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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from a2a.types import Part, DataPart
try:
    from a2ui.a2a import create_a2ui_part
except ImportError:
    # Fallback: build a basic A2UI Part when the SDK is not installed
    def create_a2ui_part(a2ui_data: dict) -> Part:  # type: ignore[misc]
        return Part(root=DataPart(
            data=a2ui_data,
            metadata={"mimeType": "application/json+a2ui"},
        ))

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("A2UIRegistry")


# ---------------------------------------------------------------------------
# Render Context — the single input every component receives
# ---------------------------------------------------------------------------

@dataclass
class RenderContext:
    """
    Bundles all information needed to decide which component renders a response.
    """

    content: Any = ""
    status: str = "working"
    is_task_complete: bool = False
    require_user_input: bool = False
    response_type: str = "text"
    metadata: Dict[str, Any] = field(default_factory=dict)
    # K8s-specific extensions
    phase_override: Optional[str] = None 
    agent_name: Optional[str] = None
    use_ui: bool = False
    session_id: Optional[str] = None
    task_id: Optional[str] = None

    # ---------- convenience helpers ----------

    @property
    def content_str(self) -> str:
        """Return *content* as a display-friendly string."""
        if isinstance(self.content, dict):
            return (
                self.content.get("summary")
                or self.content.get("message")
                or self.content.get("question")
                or str(self.content)
            )
        return str(self.content) if self.content else "Processing..."

    @property
    def phase(self) -> str:
        """Extract the workflow phase from metadata or content."""
        if self.phase_override:
            return self.phase_override
        if isinstance(self.content, dict):
            return (
                self.content.get("phase")
                or self.content.get("active_phase")
                or self.metadata.get("phase", "unknown")
            )
        return self.metadata.get("phase", "unknown")


# ---------------------------------------------------------------------------
# Base Component
# ---------------------------------------------------------------------------

class BaseComponent(ABC):
    """
    Abstract base class for every A2UI component.

    Subclasses MUST set ``component_type`` to a unique identifier.
    Optionally set ``catalog_id`` to tie the component to a custom catalog.
    """

    component_type: str = ""
    catalog_id: Optional[str] = None

    @abstractmethod
    def can_handle(self, ctx: RenderContext) -> bool:
        """Return True if this component should render the given context."""
        ...

    @abstractmethod
    def build(self, ctx: RenderContext) -> List[dict]:
        """
        Build and return a list of A2UI message dicts
        (beginRendering, surfaceUpdate, dataModelUpdate, …).
        """
        ...

    def build_parts(self, ctx: RenderContext) -> List[Part]:
        """Build A2UI messages and convert each to an A2A ``Part``."""
        return [create_a2ui_part(msg) for msg in self.build(ctx)]


# ---------------------------------------------------------------------------
# Component Registry
# ---------------------------------------------------------------------------

class ComponentRegistry:
    """
    Central registry for A2UI components.

    Components are tried in **descending priority** order.  The first
    component whose ``can_handle`` returns True renders the response.
    """

    def __init__(self) -> None:
        # (priority, component) — higher priority = checked first
        self._components: List[tuple[int, BaseComponent]] = []
        self._type_index: Dict[str, BaseComponent] = {}

    # ---- registration ----------------------------------------------------

    def register(
        self,
        component: BaseComponent,
        priority: int = 0,
    ) -> None:
        """Add a component to the registry."""
        if not component.component_type:
            raise ValueError(
                f"{component.__class__.__name__} must define a non-empty "
                f"'component_type' class attribute."
            )
        self._components.append((priority, component))
        self._components.sort(key=lambda t: t[0], reverse=True)
        self._type_index[component.component_type] = component
        logger.debug(
            f"Registered component {component.component_type} (priority={priority})"
        )

    # ---- dispatch --------------------------------------------------------

    def build_parts(self, ctx: RenderContext) -> List[Part]:
        """
        Find the first matching component and build A2A Parts.

        Falls back to a plain ``TextPart`` when no component matched.
        """
        for _priority, component in self._components:
            if component.can_handle(ctx):
                logger.debug(
                    f"Dispatching to component {component.component_type}"
                )
                return component.build_parts(ctx)

        # Fallback — no component matched
        from a2a.types import TextPart
        logger.warning(
            f"No component matched for context (status={ctx.status}, response_type={ctx.response_type})"
        )
        return [
            Part(root=TextPart(text=ctx.content_str))
        ]

    # ---- introspection ---------------------------------------------------

    def get_component(self, component_type: str) -> Optional[BaseComponent]:
        return self._type_index.get(component_type)

    def list_components(self) -> List[str]:
        return [comp.component_type for _p, comp in self._components]

    def list_components_detailed(self) -> List[Dict[str, Any]]:
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

_registry: Optional[ComponentRegistry] = None


def get_registry() -> ComponentRegistry:
    """Return (or create) the global ``ComponentRegistry`` singleton."""
    global _registry
    if _registry is None:
        _registry = ComponentRegistry()
        # Auto-import standard components so they self-register
        _auto_discover_components()
    return _registry


def _auto_discover_components() -> None:
    """Import the ``components`` sub-package so decorators fire."""
    try:
        import importlib
        modules = [
            "k8s_autopilot.core.a2ui.components.working_status",
            "k8s_autopilot.core.a2ui.components.completion",
            "k8s_autopilot.core.a2ui.components.k8s_operator_approval",
            "k8s_autopilot.core.a2ui.components.app_operator_approval",
            "k8s_autopilot.core.a2ui.components.observability_operator_approval",
            "k8s_autopilot.core.a2ui.components.hitl_approval",
            "k8s_autopilot.core.a2ui.components.values_confirmation",
            "k8s_autopilot.core.a2ui.components.error",
            "k8s_autopilot.core.a2ui.components.info_message",
            "k8s_autopilot.core.a2ui.components.user_input",
            "k8s_autopilot.core.a2ui.components.chat_continue",
        ]
        for mod in modules:
            try:
                importlib.import_module(mod)
            except ImportError as e:
                logger.warning(f"Failed to import A2UI component: {mod}: {e}")
    except Exception as e:
        logger.error(f"Error discovering A2UI components: {e}")


# ---------------------------------------------------------------------------
# Decorator for declarative registration
# ---------------------------------------------------------------------------

def register_component(
    priority: int = 0,
) -> Callable[[Type[BaseComponent]], Type[BaseComponent]]:
    """
    Class decorator that instantiates and registers a ``BaseComponent``.
    """
    def decorator(cls: Type[BaseComponent]) -> Type[BaseComponent]:
        instance = cls()
        get_registry().register(instance, priority=priority)
        return cls

    return decorator
