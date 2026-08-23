"""Central Agent Middleware Registry for k8s-autopilot.

Provides a pluggable middleware registry where custom deep agent middleware
classes are registered declaratively using decorators and instantiated explicitly by name.
"""

import importlib
import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar

from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("MiddlewareRegistry")


# ---------------------------------------------------------------------------
# Base Middleware Class
# ---------------------------------------------------------------------------

class BaseAgentMiddleware(AgentMiddleware, ABC):
    """Abstract base class for all custom deep agent middleware in k8s-autopilot."""
    pass


# ---------------------------------------------------------------------------
# Registry Interfaces
# ---------------------------------------------------------------------------

class AbstractMiddlewareRegistry(ABC):
    """Abstract interface defining the contract for MiddlewareRegistry."""

    @abstractmethod
    def register(
        self,
        middleware_cls: Type[BaseAgentMiddleware],
        name: str,
    ) -> None:
        """Register a middleware class under a unique name."""
        pass

    @abstractmethod
    def build_middleware(
        self,
        name: str,
        **kwargs: Any,
    ) -> AgentMiddleware:
        """Instantiate a registered middleware class by name."""
        pass

    @abstractmethod
    def build_middlewares(
        self,
        specs: List[str | tuple[str, Dict[str, Any]]],
        **kwargs: Any,
    ) -> List[AgentMiddleware]:
        """Instantiate a list of middlewares in the exact sequence requested."""
        pass


class MiddlewareRegistry(AbstractMiddlewareRegistry):
    """Concrete implementation of the MiddlewareRegistry."""

    def __init__(self) -> None:
        self._registry: Dict[str, Type[BaseAgentMiddleware]] = {}

    def register(
        self,
        middleware_cls: Type[BaseAgentMiddleware],
        name: str,
    ) -> None:
        if not name:
            raise ValueError("Middleware name cannot be empty")

        self._registry[name] = middleware_cls
        logger.debug(f"Registered middleware '{name}'")

    def build_middleware(
        self,
        name: str,
        **kwargs: Any,
    ) -> AgentMiddleware:
        if name not in self._registry:
            raise KeyError(f"Middleware '{name}' is not registered in the registry.")

        cls = self._registry[name]
        try:
            if cls.__init__ is object.__init__:
                instance = cls()
            else:
                sig = inspect.signature(cls.__init__)
                init_args = {}
                for param_name, param in sig.parameters.items():
                    if param_name == "self" or param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                        continue
                    if param_name in kwargs:
                        init_args[param_name] = kwargs[param_name]
                    elif param.default is inspect.Parameter.empty:
                        init_args[param_name] = None

                if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    instance = cls(**kwargs)
                else:
                    instance = cls(**init_args)

            logger.debug(f"Instantiated custom middleware '{name}'")
            return instance
        except Exception as e:
            logger.error(f"Failed to instantiate middleware '{name}': {e}")
            raise

    def build_middlewares(
        self,
        specs: List[str | tuple[str, Dict[str, Any]]],
        **kwargs: Any,
    ) -> List[AgentMiddleware]:
        instances: List[AgentMiddleware] = []
        for spec in specs:
            if isinstance(spec, str):
                name = spec
                custom_args = {}
            elif isinstance(spec, tuple) and len(spec) == 2:
                name, custom_args = spec
            else:
                raise TypeError(f"Invalid middleware spec format: {spec}")

            merged_args = {**kwargs, **custom_args}
            instances.append(self.build_middleware(name, **merged_args))
        return instances


# ---------------------------------------------------------------------------
# Global Singleton and Decorator
# ---------------------------------------------------------------------------

_registry: Optional[MiddlewareRegistry] = None


def get_middleware_registry() -> MiddlewareRegistry:
    """Return the global MiddlewareRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = MiddlewareRegistry()
        _auto_discover_middlewares()
    return _registry


T = TypeVar("T", bound=BaseAgentMiddleware)


def register_middleware(
    name: str,
) -> Callable[[Type[T]], Type[T]]:
    """Decorator to register a custom agent middleware class."""
    def decorator(cls: Type[T]) -> Type[T]:
        get_middleware_registry().register(cls, name=name)
        return cls
    return decorator


def _auto_discover_middlewares() -> None:
    """Import middleware modules to trigger registration decorators."""
    modules = [
        "k8s_autopilot.core.middleware.deduplication",
        "k8s_autopilot.core.middleware.plan_lock",
        "k8s_autopilot.core.middleware.a2ui_buffer",
        "k8s_autopilot.core.middleware.thought_signature_fix",
        "k8s_autopilot.core.middleware.skill_shortcut",
        "k8s_autopilot.core.middleware.operation_context",
        "k8s_autopilot.core.middleware.local_context",
        "k8s_autopilot.core.middleware.shell_allow_list",
        "k8s_autopilot.core.middleware.goal_tools",
        "k8s_autopilot.core.middleware.ask_user",
        "k8s_autopilot.core.middleware.configurable_model",
        "k8s_autopilot.core.middleware.resume_state",
        "k8s_autopilot.core.middleware.memory_guard",
        # ── New enhancement modules ───────────────────────────────────────
        "k8s_autopilot.core.mcp.middleware",
        "k8s_autopilot.core.rubrics.middleware",
        "k8s_autopilot.core.middleware.compaction",
        "k8s_autopilot.core.middleware.human_in_the_loop",
    ]
    for module in modules:
        try:
            importlib.import_module(module)
        except ImportError as e:
            logger.warning(f"Failed to auto-discover middleware module {module}: {e}")


# ---------------------------------------------------------------------------
# Declarative Core Subagent Middlewares
# ---------------------------------------------------------------------------

class BaseDelegatingMiddleware(BaseAgentMiddleware):
    """Base middleware that delegates all lifecycle hooks to an inner middleware instance."""

    def __init__(self, inner: Optional[Any] = None) -> None:
        super().__init__()
        self._inner = inner

        if inner and hasattr(inner, "state_schema"):
            self.state_schema = inner.state_schema
        self.tools = inner.tools if (inner and hasattr(inner, "tools")) else []
        self.transformers = inner.transformers if (inner and hasattr(inner, "transformers")) else ()

    @property
    def name(self) -> str:
        inner = self._inner_any
        if inner and hasattr(inner, "name"):
            return inner.name
        return super().name

    @property
    def _inner_any(self) -> Any:
        return self._inner

    def _is_overridden(self, method_name: str) -> bool:
        inner = self._inner_any
        if not inner:
            return False
        if not hasattr(inner, method_name):
            return False
        from langchain.agents.middleware import AgentMiddleware
        base_method = getattr(AgentMiddleware, method_name, None)
        if base_method is not None:
            return getattr(inner.__class__, method_name) is not base_method
        return True

    def before_agent(self, state: Any, runtime: Any, config: Optional[RunnableConfig] = None) -> Any:
        inner = self._inner_any
        if self._is_overridden("before_agent"):
            import inspect
            sig = inspect.signature(inner.before_agent)
            if "config" in sig.parameters:
                return inner.before_agent(state, runtime, config)
            else:
                return inner.before_agent(state, runtime)
        return state

    async def abefore_agent(self, state: Any, runtime: Any, config: Optional[RunnableConfig] = None) -> Any:
        inner = self._inner_any
        if self._is_overridden("abefore_agent"):
            import inspect
            sig = inspect.signature(inner.abefore_agent)
            if "config" in sig.parameters:
                return await inner.abefore_agent(state, runtime, config)
            else:
                return await inner.abefore_agent(state, runtime)
        return state

    def modify_request(self, request: Any, handler: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("modify_request"):
            return inner.modify_request(request, handler)
        return handler(request)

    async def amodify_request(self, request: Any, handler: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("amodify_request"):
            return await inner.amodify_request(request, handler)
        return await handler(request)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("wrap_model_call"):
            return inner.wrap_model_call(request, handler)
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("awrap_model_call"):
            return await inner.awrap_model_call(request, handler)
        return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("wrap_tool_call"):
            return inner.wrap_tool_call(request, handler)
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("awrap_tool_call"):
            return await inner.awrap_tool_call(request, handler)
        return await handler(request)

    def after_agent(self, state: Any, runtime: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("after_agent"):
            return inner.after_agent(state, runtime)
        return state

    async def aafter_agent(self, state: Any, runtime: Any) -> Any:
        inner = self._inner_any
        if self._is_overridden("aafter_agent"):
            return await inner.aafter_agent(state, runtime)
        return state


@register_middleware(name="skills")
class SkillsMiddleware(BaseDelegatingMiddleware):
    def __init__(self, sources: list[str]) -> None:
        from pathlib import Path
        from deepagents.middleware import SkillsMiddleware as DeepSkillsMiddleware
        from k8s_autopilot.core.backend import get_project_root, K8sBackendMixin

        root = get_project_root()
        source_dirs: list[str] = []
        for skill_path in sources:
            if skill_path.startswith("/skills/"):
                source_dirs.append(skill_path)
                continue

            abs_path = root / skill_path if not skill_path.startswith("/") else Path(skill_path)
            if abs_path.is_dir() and (abs_path.name == "skills" or (abs_path / "skills").is_dir()):
                try:
                    rel_path = abs_path.relative_to(root)
                    parts = rel_path.parts
                    if parts and parts[0] == "plugins":
                        filtered = [p for p in parts[1:] if p not in ("agents", "skills")]
                        source_dirs.append("/skills/" + "/".join(filtered))
                    elif parts and parts[0] == "skills":
                        source_dirs.append("/skills/" + "/".join(parts[1:]))
                    else:
                        source_dirs.append(str(abs_path))
                except Exception:
                    source_dirs.append(str(abs_path))
            else:
                logger.debug(f"skills: path not found or contains no skills/ subfolder: {abs_path}")

        if source_dirs:
            inner = DeepSkillsMiddleware(
                backend=K8sBackendMixin.make_backend(),
                sources=sorted(set(source_dirs)),
            )
        else:
            inner = None
        super().__init__(inner)


@register_middleware(name="memory")
class MemoryMiddleware(BaseDelegatingMiddleware):
    def __init__(self, sources: list[str]) -> None:
        from pathlib import Path
        from deepagents.middleware import MemoryMiddleware as DeepMemoryMiddleware
        from deepagents.backends.filesystem import FilesystemBackend
        from k8s_autopilot.core.backend import get_project_root

        root = get_project_root()
        physical_sources: list[str] = []
        for mem_path in sources:
            if mem_path.startswith("/memories/"):
                from k8s_autopilot.core.memory import get_memory_registry
                phys = get_memory_registry().resolve_virtual_path(mem_path)
            else:
                phys = root / mem_path if not mem_path.startswith("/") else Path(mem_path)

            if phys and phys.is_file():
                physical_sources.append(str(phys))
            else:
                logger.warning(f"memory: path not found: {phys}")

        if physical_sources:
            inner = DeepMemoryMiddleware(
                backend=FilesystemBackend(virtual_mode=False),
                sources=sorted(set(physical_sources)),
            )
        else:
            inner = None
        super().__init__(inner)


@register_middleware(name="ptc_interpreter")
class PTCInterpreterMiddleware(BaseDelegatingMiddleware):
    def __init__(self, allowlist: list[str]) -> None:
        from k8s_autopilot.core.agents.shared_middleware import build_code_interpreter_middleware

        mws = build_code_interpreter_middleware(ptc_allowlist=allowlist)
        inner = mws[0] if mws else None
        super().__init__(inner)

