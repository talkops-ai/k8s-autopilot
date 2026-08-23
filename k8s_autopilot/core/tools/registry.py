import inspect
from typing import Any, Callable, Dict, List
from langchain_core.tools import BaseTool

class ToolRegistry:
    """Registry for managing and building extra tools dynamically."""

    def __init__(self) -> None:
        self._registry: Dict[str, Callable[..., Any]] = {}

    def register(self, name: str, factory: Callable[..., Any]) -> None:
        """Register a tool factory function."""
        self._registry[name] = factory

    def build_tool(self, name: str, **kwargs: Any) -> BaseTool:
        """Build an instance of the registered tool by name with arguments."""
        if name not in self._registry:
            raise KeyError(f"Tool '{name}' is not registered in the tool registry.")

        factory = self._registry[name]
        # Inspect factory signature to pass only valid arguments
        sig = inspect.signature(factory)
        init_args = {}
        for param_name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            if param_name in kwargs:
                init_args[param_name] = kwargs[param_name]

        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return factory(**kwargs)
        else:
            return factory(**init_args)

    def list_tools(self) -> List[str]:
        """List registered tool names."""
        return list(self._registry.keys())


_GLOBAL_TOOL_REGISTRY = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    """Get the global tool registry instance."""
    return _GLOBAL_TOOL_REGISTRY


# Register default tools
def _register_default_tools() -> None:
    from k8s_autopilot.core.tools.kubectl_tools import create_kubectl_readonly_tool
    from k8s_autopilot.core.tools.websearch_tool import (
        create_get_current_thread_id_tool,
        create_web_search_tool,
        create_fetch_url_tool,
    )

    get_tool_registry().register("kubectl_readonly", create_kubectl_readonly_tool)
    get_tool_registry().register("get_current_thread_id", create_get_current_thread_id_tool)
    get_tool_registry().register("web_search", create_web_search_tool)
    get_tool_registry().register("fetch_url", create_fetch_url_tool)


_register_default_tools()
