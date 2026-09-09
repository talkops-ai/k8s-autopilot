"""Server-side graph entry point for K8s Autopilot.

Provides `make_graph()` factory that loads `ServerConfig.from_env()`, builds
the complete K8s Autopilot agent graph, and returns the compiled Pregel graph.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import sys
from typing import Any

from k8s_autopilot.server._server_config import ServerConfig
from k8s_autopilot.utils.logger import get_logger
from k8s_autopilot.utils.startup_error import emit_startup_failure

logger = get_logger(__name__)


async def _make_graph() -> Any:
    """Create the agent graph from environment-based configuration.

    All initialization runs inside a worker thread so that synchronous code
    (e.g. MCP session creation via asyncio.run) never collides with the
    running uvicorn event loop.
    """

    def _make_graph_sync() -> Any:
        from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
        from k8s_autopilot.config.settings import get_settings
        from k8s_autopilot.model.factory import create_model
        from k8s_autopilot.tools.catalog import register_all_tools
        from k8s_autopilot.tools.registry import ToolRegistry

        config = ServerConfig.from_env()
        settings = get_settings()

        model_spec = config.model or getattr(settings, "model_name", None) or "gemini-3.7-flash"
        model_res = create_model(model_spec)
        model_res.apply_to_settings()

        register_all_tools()
        registry = ToolRegistry.get_instance()

        tools: list[Any] = []
        for tool_name in ["web_search", "fetch_url"]:
            try:
                tools.append(registry.build_tool(tool_name))
            except Exception as e:
                logger.warning("Failed to build registered tool %s: %s", tool_name, e)

        agent, _composite_backend = create_k8s_autopilot_agent(
            model=model_res.model,
            assistant_id=config.assistant_id,
            tools=tools,
            system_prompt=config.system_prompt,
            interactive=config.interactive,
            auto_approve=config.auto_approve,
            enable_shell=config.enable_shell,
            enable_interpreter=config.enable_interpreter,
            cwd=config.cwd,
        )
        return agent

    return await asyncio.to_thread(_make_graph_sync)


def _build_graph_factory(
    builder: Callable[[], Awaitable[Any]] | None = None,
) -> Callable[[], Awaitable[Any]]:
    missing = object()
    graph: Any = missing
    lock = asyncio.Lock()

    async def make_graph() -> Any:
        """Create and cache the compiled LangGraph server instance.

        Returns:
            Any: Compiled graph instance or dummy graph on missing credentials.
        """
        nonlocal graph
        if graph is not missing:
            return graph
        async with lock:
            if graph is missing:
                try:
                    graph = await (builder or _make_graph)()
                except Exception as exc:
                    emit_startup_failure(exc)
                    exc_str = str(exc).lower()
                    is_credential_error = any(
                        kw in exc_str
                        for kw in (
                            "api_key",
                            "api key",
                            "credential",
                            "authentication",
                            "unauthorized",
                            "missing",
                        )
                    )
                    if is_credential_error:
                        raise
                    sys.exit(1)
            return graph

    return make_graph


make_graph = _build_graph_factory()
