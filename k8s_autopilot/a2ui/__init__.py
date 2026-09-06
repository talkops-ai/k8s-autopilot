# A2UI Support Module for K8s Autopilot
# Provides programmatic A2UI building for agent responses

from .schema import A2UI_SCHEMA
from .registry import get_registry, register_component, BaseComponent
from .catalog_manager import get_catalog_manager

__all__ = [
    "A2UI_SCHEMA",
    "get_registry",
    "register_component",
    "BaseComponent",
    "get_catalog_manager",
]
