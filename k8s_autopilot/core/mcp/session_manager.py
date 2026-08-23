"""
Startup MCP Session Manager for deep agent coordinators.

Replaces the legacy JIT ``create_mcp_client`` per-subagent-call pattern
with a single session manager that resolves all domain-bound MCP tools at
``build_agent()`` time.  Tools are then injected into subagent specs
before ``create_deep_agent()`` is called.

Key design decisions:
    - **Graceful degradation**: If a server is unreachable, the manager logs
      a warning and continues with the remaining servers.  The agent is never
      crashed by an MCP connection failure.
    - **Lazy sessions**: Connections are opened on first ``resolve_tools()``
      call, not at construction time.
    - **Domain binding**: Uses ``DomainMCPBinding`` to restrict tools per domain.
    - **Reusable**: Works for any coordinator domain, not just Helm.

Lifecycle::

    manager = MCPSessionManager(config, domain="helm-operator")
    async with manager:
        tools = await manager.resolve_tools()
        # ... build agent with tools ...
    # sessions auto-cleaned up

Reference: dcode/code/mcp_tools.py MCPSessionManager
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool

from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("MCPSessionManager")


# ---------------------------------------------------------------------------
# Server connection result — tracks per-server health
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MCPServerResult:
    """Outcome of connecting to a single MCP server."""

    name: str
    status: str  # "ok" | "error"
    tools: tuple[BaseTool, ...] = ()
    error: str | None = None


# ---------------------------------------------------------------------------
# MCPSessionManager
# ---------------------------------------------------------------------------

class MCPSessionManager:
    """Manages MCP server sessions for a coordinator's lifetime.

    Usage::

        manager = MCPSessionManager(config, domain="helm-operator")
        async with manager:
            tools, results = await manager.resolve_tools()
            helm_tools = manager.filter_tools_by_server(["helm_mcp_server"])
            # ... pass tools to create_deep_agent ...

    Graceful degradation:
        - If a server fails to connect, ``MCPServerResult.status`` is ``"error"``
          and ``MCPServerResult.error`` contains the message.
        - The agent continues with tools from the servers that did connect.
        - The ``error_summary`` property provides a human-readable status report.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        domain: str | None = None,
        server_filter: list[str] | None = None,
    ) -> None:
        if config is None:
            from k8s_autopilot.config.config import Config as _Config
            config = _Config()

        self._config = config
        self._domain = domain
        self._explicit_filter = server_filter
        self._exit_stack: AsyncExitStack | None = None
        self._tools: list[BaseTool] = []
        self._server_results: list[MCPServerResult] = []
        self._server_tool_map: dict[str, list[BaseTool]] = {}
        self._server_clients: dict[str, Any] = {}
        self._resolved = False
        self._closed = False

    # ── Async context manager ─────────────────────────────────────────

    async def __aenter__(self) -> MCPSessionManager:
        self._exit_stack = AsyncExitStack()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.cleanup()

    async def cleanup(self) -> None:
        """Close all MCP sessions.  Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True

        if self._exit_stack:
            try:
                await asyncio.wait_for(self._exit_stack.aclose(), timeout=10.0)
            except TimeoutError:
                logger.warning("MCP session cleanup timed out after 10s")
            except Exception as exc:
                logger.warning(f"MCP session cleanup failed: {exc}")

        server_names = [r.name for r in self._server_results]
        logger.info(
            f"MCPSessionManager closed ({len(server_names)} server(s): {server_names})"
        )

    # ── Tool resolution ───────────────────────────────────────────────

    async def resolve_tools(self) -> tuple[list[BaseTool], list[MCPServerResult]]:
        """Connect to all domain-bound MCP servers and resolve their tools.

        Returns:
            Tuple of (all_tools, per_server_results).  Even if some servers
            fail, the tools from successful servers are returned.
        """
        if self._resolved:
            return self._tools, self._server_results

        server_configs = self._discover_servers()

        # Connect to all servers concurrently
        async def _connect_and_store(server_cfg: dict[str, Any]) -> MCPServerResult:
            return await self._connect_server(server_cfg)

        results = await asyncio.gather(*[_connect_and_store(cfg) for cfg in server_configs])

        for result in results:
            self._server_results.append(result)
            if result.status == "ok":
                tools_list = list(result.tools)
                self._tools.extend(tools_list)
                self._server_tool_map[result.name] = tools_list

        self._resolved = True

        ok_count = sum(1 for r in self._server_results if r.status == "ok")
        err_count = len(self._server_results) - ok_count
        logger.info(
            f"MCPSessionManager: resolved {len(self._tools)} tool(s) from "
            f"{ok_count} server(s) ({err_count} failed)"
        )

        return self._tools, self._server_results

    def filter_tools_by_server(
        self,
        server_names: list[str] | tuple[str, ...],
    ) -> list[BaseTool]:
        """Return only tools from the specified MCP servers.

        Used to inject a subset of tools into a specific subagent.
        """
        filtered: list[BaseTool] = []
        for name in server_names:
            filtered.extend(self._server_tool_map.get(name, []))
        return filtered

    def create_resource_reader(
        self,
        server_name: str,
        *,
        description: str | None = None,
    ) -> BaseTool:
        """Create a ``read_mcp_resource`` tool parameterised by server name.

        Returns a ``StructuredTool`` that reads MCP resources from the
        specified server.  Falls back to a no-op tool if the server's
        client is not available (graceful degradation).
        """
        client = self._server_clients.get(server_name)
        _server = server_name

        async def read_mcp_resource(uri: str) -> str:
            """Read content of a specific MCP resource by URI."""
            if client is None:
                return (
                    f"Error: MCP server '{_server}' is not connected. "
                    "The server may be unreachable or failed to start."
                )
            try:
                res = await client.read_resource(uri, server_name=_server)
                if hasattr(res, "contents") and res.contents:
                    for item in res.contents:
                        if hasattr(item, "text"):
                            return item.text
                return str(res)
            except Exception as e:
                return f"Error reading resource {uri}: {e}"

        desc = description or (
            f"Read content of a specific MCP resource by URI "
            f"(server: {_server}). Use this to read state natively."
        )

        return StructuredTool.from_function(
            func=None,
            coroutine=read_mcp_resource,
            name="read_mcp_resource",
            description=desc,
        )

    @property
    def error_summary(self) -> str | None:
        """Human-readable summary of any server connection failures.

        Returns ``None`` if all servers connected successfully.
        """
        errors = [r for r in self._server_results if r.status == "error"]
        if not errors:
            return None
        lines = [f"- {r.name}: {r.error}" for r in errors]
        return (
            f"{len(errors)} MCP server(s) failed to connect:\n"
            + "\n".join(lines)
        )

    @property
    def all_ok(self) -> bool:
        """True if all configured servers connected successfully."""
        return all(r.status == "ok" for r in self._server_results)

    # ── Internal helpers ──────────────────────────────────────────────

    def _discover_servers(self) -> list[dict[str, Any]]:
        """Discover and filter MCP servers for this domain."""
        from k8s_autopilot.core.mcp.discovery import MCPDiscovery
        from k8s_autopilot.core.mcp.trust import MCPTrustStore
        from k8s_autopilot.core.backend import get_project_root

        # Discover project-level servers
        trust = MCPTrustStore()
        discovery = MCPDiscovery(trust_store=trust)
        discovered = discovery.discover(project_root=get_project_root())

        # Merge with default config servers
        default_servers = self._config.mcp_config.get("servers", [])
        merged = discovery.merge_with_defaults(default_servers, discovered)

        # Apply explicit filter first (highest priority)
        if self._explicit_filter:
            filter_set = set(self._explicit_filter)
            merged = [s for s in merged if s.get("name") in filter_set]
            logger.debug(
                f"Explicit server filter applied: {self._explicit_filter} "
                f"→ {len(merged)} server(s)"
            )
            return merged

        return merged

    async def _connect_server(
        self,
        server_cfg: dict[str, Any],
    ) -> MCPServerResult:
        """Attempt to connect to a single MCP server.

        Returns ``MCPServerResult`` with either tools or an error message.
        Never raises.
        """
        name = server_cfg.get("name", "unknown")

        try:
            from k8s_autopilot.utils.mcp_client import MCPClient

            client = MCPClient(self._config, server_filter=[name])
            ctx = client.connect()
            
            # Enforce a 5.0 second connection timeout to prevent hanging
            await asyncio.wait_for(
                self._exit_stack.enter_async_context(ctx),  # type: ignore[union-attr]
                timeout=5.0
            )

            tools = client.get_tools()
            self._server_clients[name] = client

            logger.info(
                f"MCPSessionManager: connected to '{name}' "
                f"({len(tools)} tool(s))"
            )
            return MCPServerResult(
                name=name,
                status="ok",
                tools=tuple(tools),
            )

        except (asyncio.TimeoutError, TimeoutError) as exc:
            error_msg = "Connection timed out after 5.0 seconds"
            logger.warning(f"MCP server '{name}' connection timed out: {error_msg}")
            return MCPServerResult(
                name=name,
                status="error",
                error=error_msg,
            )

        except Exception as exc:
            error_msg = str(exc)

            # Classify the error for helpful reporting
            if any(kw in error_msg.lower() for kw in (
                "authentication", "401", "403", "unauthorized",
                "forbidden", "expired",
            )):
                classified = (
                    f"Authentication failed for MCP server '{name}'. "
                    "Check credentials and environment variables."
                )
            elif any(kw in error_msg.lower() for kw in (
                "connection refused", "unreachable", "timeout",
                "econnrefused", "enotfound",
            )):
                classified = (
                    f"MCP server '{name}' is unreachable. "
                    "Check that the server is running and accessible."
                )
            else:
                classified = f"MCP server '{name}' failed to connect: {error_msg}"

            logger.warning(classified)
            return MCPServerResult(
                name=name,
                status="error",
                error=classified,
            )
