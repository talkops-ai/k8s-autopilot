"""Agent-to-UI (A2UI) rendering components and surface builders."""

from .catalog_manager import get_catalog_manager
from .registry import BaseComponent, get_registry, register_component
from .schema import A2UI_SCHEMA

__all__ = [
    "A2UI_SCHEMA",
    "BaseComponent",
    "get_catalog_manager",
    "get_registry",
    "register_component",
]
