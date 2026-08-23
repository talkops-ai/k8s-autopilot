"""
MCP (Model Context Protocol) Client for K8s Autopilot Agent.

Manages sessions to one-or-many MCP servers, exposing their tools, resources,
and prompts as LangChain-compatible objects.

Enhanced with dcode patterns:
  - ``MCPSessionManager`` for lazy per-server session creation
  - ``_is_transient_session_error()`` for dead-session detection
  - Retry-once on transient errors in tool execution
  - Cursor-based paginated tool discovery
  - ``_normalize_mcp_arguments()`` — schema-aware empty-string drop
  - ``allowedTools`` / ``disabledTools`` filtering with fnmatch globs
  - Pre-flight health checks (command-on-PATH, HTTP HEAD probes)
  - Transport-specific config validation
  - JSON error diagnostics with hints and caret snippets

Design principles:
    • **No global monkey-patching** — timeout config is passed per-client.
    • **Lazy session lifecycle** — sessions created on first tool call.
    • **Tool error resilience** — every wrapped tool has ``handle_tool_error=True``.
    • **Clean shutdown** — ``cleanup()`` closes all sessions with 5s timeout.

Reference: dcode/code/mcp_tools.py
"""


import asyncio
import fnmatch
import json
import os
import re
import shutil
import yaml
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, AsyncExitStack
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.messages import (
    BaseMessage,
    SystemMessage,
    HumanMessage,
    AIMessage,
)

from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.exceptions import MCPClientError

if TYPE_CHECKING:
    from mcp import ClientSession
    from k8s_autopilot.config.config import Config

logger = AgentLogger("MCPClient")

# Default tool execution timeout (seconds).  Overridable via Config.
_DEFAULT_TOOL_TIMEOUT: float = 300.0


# ===========================================================================
# Transient error detection (adapted from dcode)
# ===========================================================================

def _is_transient_session_error(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` signals the MCP session transport is dead.

    Adapted from dcode's ``_is_transient_session_error()``. Detects broken
    pipes, EOF, and anyio resource errors that indicate the underlying
    stdio subprocess or HTTP connection died.
    """
    try:
        import anyio
    except ImportError:
        anyio_excs: tuple[type[BaseException], ...] = ()
    else:
        anyio_excs = (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            anyio.EndOfStream,
        )
    return isinstance(
        exc,
        (
            *anyio_excs,
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionResetError,
            EOFError,
            asyncio.IncompleteReadError,
        ),
    )


# ===========================================================================
# MCPSessionEntry — cached session + close stack
# ===========================================================================

@dataclass(frozen=True, slots=True)
class _MCPSessionEntry:
    """Cached MCP session and its close stack."""
    session: "ClientSession"
    exit_stack: AsyncExitStack


# ===========================================================================
# MCPSessionManager — lazy, per-server session cache (dcode pattern)
# ===========================================================================

class MCPSessionManager:
    """Lazy, per-server cache of persistent MCP sessions.

    Adapted from dcode's ``MCPSessionManager``. Discovery happens through
    throwaway sessions. Live sessions are only created on the first real
    tool call so sessions stay bound to the loop that owns their
    subprocess/transport handles.

    Key features vs our old eager ``connect()`` approach:
      - Per-server ``asyncio.Lock`` prevents double-creation races
      - ``invalidate()`` evicts dead sessions for transparent re-creation
      - ``cleanup()`` closes all sessions concurrently with 5s timeout
    """

    def __init__(
        self,
        *,
        connections: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._connections: dict[str, dict[str, Any]] = dict(connections or {})
        self._entries: dict[str, _MCPSessionEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    def configure(self, connections: dict[str, dict[str, Any]]) -> None:
        """Set or update connection configs.

        When no sessions exist yet, overwrites unconditionally. Once
        sessions are active, validates compatibility before accepting.
        """
        if self._closed:
            raise RuntimeError("Cannot configure a closed MCP session manager")

        if not self._entries:
            self._connections = dict(connections)
            return

        # If sessions exist, only accept compatible reconfiguration
        # (same server names — content may differ for reconnection)
        if set(self._connections) != set(connections):
            raise RuntimeError(
                "Cannot reconfigure MCP session manager after sessions "
                "are active with different server names"
            )
        self._connections = dict(connections)

    async def get_session(self, server_name: str) -> "ClientSession":
        """Return a cached session, creating it lazily on first access."""
        entry = self._entries.get(server_name)
        if entry is not None:
            return entry.session

        lock = self._get_lock(server_name)
        async with lock:
            # Double-check after acquiring lock
            entry = self._entries.get(server_name)
            if entry is not None:
                return entry.session

            entry = await self._create_entry(server_name)
            self._entries[server_name] = entry
            return entry.session

    async def invalidate(
        self,
        server_name: str,
        *,
        expected_session: "ClientSession | None" = None,
    ) -> None:
        """Evict and close a cached session.

        Args:
            server_name: MCP server name.
            expected_session: If provided, only evict if the cached session
                matches (prevents race conditions).
        """
        lock = self._get_lock(server_name)
        async with lock:
            entry = self._entries.get(server_name)
            if entry is None:
                return
            if expected_session is not None and entry.session is not expected_session:
                return
            self._entries.pop(server_name, None)
            exit_stack = entry.exit_stack

        try:
            await exit_stack.aclose()
        except RuntimeError as err:
            if "Attempted to exit cancel scope" in str(err):
                # Harmless cross-task concurrency cleanup during shutdown or transition
                pass
            else:
                logger.warning(
                    f"Failed to close MCP session for {server_name!r}",
                    exc_info=True,
                )
        except Exception:
            logger.warning(
                f"Failed to close MCP session for {server_name!r}",
                exc_info=True,
            )

    async def cleanup(self) -> None:
        """Close all cached sessions concurrently with 5s per-server timeout."""
        if self._closed and not self._entries:
            return

        self._closed = True
        names = list(self._entries)

        async def _close(server_name: str) -> None:
            try:
                await asyncio.wait_for(
                    self.invalidate(server_name), timeout=5.0,
                )
            except TimeoutError:
                logger.warning(
                    f"MCP session cleanup for {server_name!r} timed out after 5s",
                )
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception:
                logger.warning(
                    f"MCP session cleanup for {server_name!r} failed",
                    exc_info=True,
                )

        await asyncio.gather(*[_close(name) for name in names])

    @property
    def server_names(self) -> list[str]:
        """Return configured server names."""
        return list(self._connections)

    @property
    def active_sessions(self) -> list[str]:
        """Return names of servers with active sessions."""
        return list(self._entries)

    def _get_lock(self, server_name: str) -> asyncio.Lock:
        """Return the per-server creation/eviction lock."""
        lock = self._locks.get(server_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[server_name] = lock
        return lock

    async def _create_entry(self, server_name: str) -> _MCPSessionEntry:
        """Create and initialize a new session entry."""
        if self._closed:
            raise RuntimeError("Cannot create an MCP session after cleanup")

        try:
            connection = self._connections[server_name]
        except KeyError as exc:
            raise ValueError(
                f"No MCP server named '{server_name}', "
                f"expected one of {sorted(self._connections)}"
            ) from exc

        from langchain_mcp_adapters.client import MultiServerMCPClient

        exit_stack = AsyncExitStack()
        try:
            # Use MultiServerMCPClient's internal session creation with a clean connection config
            clean_connection = {k: v for k, v in connection.items() if k != "_server_def"}
            client = MultiServerMCPClient({server_name: clean_connection})  # type: ignore[arg-type]
            session = await exit_stack.enter_async_context(
                client.session(server_name)
            )
        except Exception:
            await exit_stack.aclose()
            raise

        return _MCPSessionEntry(session=session, exit_stack=exit_stack)


# ===========================================================================
# Config validation (adapted from dcode)
# ===========================================================================

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
"""Server names must remain path-safe."""


class MCPConfigError(ValueError):
    """MCP configuration is malformed or structurally invalid."""


def _validate_server_config(server_name: str, server_config: dict[str, Any]) -> None:
    """Validate a single server configuration.

    Adapted from dcode's ``_validate_server_config()`` with transport-specific
    field validation, tool filter checks, and server name regex.

    Args:
        server_name: Server name.
        server_config: Server configuration dict.

    Raises:
        MCPConfigError: If configuration is invalid.
    """
    if not _SERVER_NAME_RE.match(server_name):
        raise MCPConfigError(
            f"Server name '{server_name}' is invalid — "
            "only letters, digits, hyphens, and underscores are allowed."
        )

    transport = server_config.get("transport", "sse")

    if transport in ("sse", "http"):
        url = server_config.get("url")
        if not url:
            raise MCPConfigError(
                f"Server '{server_name}' with transport '{transport}' "
                "is missing required 'url' field."
            )
        if not isinstance(url, str):
            raise MCPConfigError(
                f"Server '{server_name}' 'url' must be a string."
            )
        headers = server_config.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise MCPConfigError(
                f"Server '{server_name}' 'headers' must be a dictionary."
            )
        if isinstance(headers, dict):
            for name, value in headers.items():
                if not isinstance(value, str):
                    raise MCPConfigError(
                        f"Server '{server_name}' header '{name}' must be "
                        f"a string, got {type(value).__name__}"
                    )

    elif transport == "stdio":
        command = server_config.get("command")
        if not command:
            raise MCPConfigError(
                f"Server '{server_name}' with transport 'stdio' "
                "is missing required 'command' field."
            )
        args = server_config.get("args")
        if args is not None and not isinstance(args, list):
            raise MCPConfigError(
                f"Server '{server_name}' 'args' must be a list."
            )
        env = server_config.get("env")
        if env is not None and not isinstance(env, dict):
            raise MCPConfigError(
                f"Server '{server_name}' 'env' must be a dictionary."
            )
    else:
        raise MCPConfigError(
            f"Server '{server_name}' has unsupported transport '{transport}'. "
            "Supported: stdio, sse, http"
        )

    _validate_tool_filter_fields(server_name, server_config)


def _validate_tool_filter_fields(
    server_name: str,
    server_config: dict[str, Any],
) -> None:
    """Validate optional ``allowedTools`` / ``disabledTools`` fields.

    Both are mutually exclusive. When present, must be non-empty lists of strings.
    """
    has_allowed = "allowedTools" in server_config
    has_disabled = "disabledTools" in server_config
    if has_allowed and has_disabled:
        raise MCPConfigError(
            f"Server '{server_name}' cannot set both 'allowedTools' and "
            "'disabledTools' — pick one."
        )

    for field_name in ("allowedTools", "disabledTools"):
        if field_name not in server_config:
            continue
        value = server_config[field_name]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise MCPConfigError(
                f"Server '{server_name}' '{field_name}' must be a list of strings."
            )
        if not value:
            raise MCPConfigError(
                f"Server '{server_name}' '{field_name}' must be non-empty; "
                "omit the field to disable filtering."
            )


# ===========================================================================
# Pre-flight health checks (adapted from dcode)
# ===========================================================================

def _check_stdio_server(server_name: str, server_config: dict[str, Any]) -> None:
    """Verify that a stdio server's command exists on PATH.

    Raises:
        MCPClientError: If the command is not found.
    """
    command = server_config.get("command")
    if not command:
        raise MCPClientError(
            f"MCP server '{server_name}': missing 'command' in config."
        )
    if shutil.which(command) is None:
        raise MCPClientError(
            f"MCP server '{server_name}': command '{command}' not found "
            "on PATH. Install it or check your MCP config."
        )


async def _check_remote_server(server_name: str, server_config: dict[str, Any]) -> None:
    """Check network connectivity to a remote MCP server URL.

    Raises:
        MCPClientError: If the URL is unreachable or returns 5xx.
    """
    url = server_config.get("url")
    if not url:
        raise MCPClientError(
            f"MCP server '{server_name}': missing 'url' in config."
        )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.head(url)
    except ImportError:
        # httpx not installed — skip remote check (not critical)
        logger.debug(
            f"httpx not installed, skipping remote pre-flight for {server_name}",
        )
        return
    except Exception as exc:
        raise MCPClientError(
            f"MCP server '{server_name}': URL '{url}' is unreachable: {exc}. "
            "Check that the URL is correct and the server is running."
        ) from exc

    if response.status_code >= 500:
        raise MCPClientError(
            f"MCP server '{server_name}': {url} returned HTTP "
            f"{response.status_code}. Server may be down; retry later."
        )


# ===========================================================================
# Tool discovery with pagination (adapted from dcode)
# ===========================================================================

async def _discover_tools(session: "ClientSession") -> list[BaseTool]:
    """Enumerate MCP tools from a session with cursor-based pagination.

    Adapted from dcode's ``_discover_tools()``. Handles servers that
    expose hundreds of tools across multiple pages.

    Args:
        session: Initialized MCP client session.

    Returns:
        All discovered MCP tools as LangChain BaseTool objects.

    Raises:
        RuntimeError: If pagination doesn't terminate within 1000 iterations.
    """
    from langchain_mcp_adapters.tools import load_mcp_tools, convert_mcp_tool_to_langchain_tool

    # Try paginated approach first
    try:
        cursor: str | None = None
        raw_tools: list[Any] = []
        for _ in range(1000):
            page = await session.list_tools(cursor=cursor)
            if page.tools:
                raw_tools.extend(page.tools)
            if not page.nextCursor:
                break
            cursor = page.nextCursor
        else:
            raise RuntimeError(
                "Reached max of 1000 iterations while listing MCP tools; "
                "server may be returning a non-terminating cursor."
            )

        return [
            convert_mcp_tool_to_langchain_tool(session, t)
            for t in raw_tools
        ]
    except (AttributeError, TypeError):
        # Fallback for older MCP SDK versions without pagination
        return await load_mcp_tools(session)


# ===========================================================================
# Argument normalization (adapted from dcode)
# ===========================================================================

def _normalize_mcp_arguments(
    arguments: dict[str, Any],
    input_schema: Any,
) -> dict[str, Any]:
    """Drop empty-string values for optional MCP tool params.

    Adapted from dcode's ``_normalize_mcp_arguments()``. Some MCP servers
    reject ``""`` for optional ID-typed params. Treat ``""`` for
    non-required string fields as "omitted".

    Args:
        arguments: Keyword arguments from the LLM's tool call.
        input_schema: The MCP tool's ``inputSchema`` (raw JSON Schema dict).

    Returns:
        Cleaned arguments dict.
    """
    if not isinstance(input_schema, dict):
        return arguments
    required = set(input_schema.get("required") or ())
    properties = input_schema.get("properties") or {}
    cleaned: dict[str, Any] = {}

    for key, value in arguments.items():
        if value != "" or key in required:
            cleaned[key] = value
            continue
        prop = properties.get(key)
        prop_type = prop.get("type") if isinstance(prop, dict) else None
        is_string_typed = prop_type == "string" or (
            isinstance(prop_type, list) and "string" in prop_type
        )
        # Drop empty strings for string-typed optional fields,
        # untyped fields, or absent fields (model invented it)
        if isinstance(prop, dict) and not is_string_typed and prop_type is not None:
            cleaned[key] = value

    if cleaned.keys() != arguments.keys():
        dropped = sorted(set(arguments) - set(cleaned))
        logger.debug(f"MCP arg normalize: dropped empty-string keys {dropped}")
    return cleaned


# ===========================================================================
# Tool filtering (adapted from dcode)
# ===========================================================================

_GLOB_METACHARS = frozenset("*?[")


def _entry_matches_tool(entry: str, tool_name: str, prefix: str) -> bool:
    """Return True if a filter entry matches a tool name.

    Supports literal names and fnmatch glob patterns. Each entry is
    tried against both the bare name and the server-prefixed form.
    """
    is_glob = any(ch in _GLOB_METACHARS for ch in entry)
    if is_glob:
        if fnmatch.fnmatchcase(tool_name, entry):
            return True
        if tool_name.startswith(prefix):
            return fnmatch.fnmatchcase(tool_name[len(prefix):], entry)
        return False
    if tool_name == entry:
        return True
    return tool_name.startswith(prefix) and tool_name[len(prefix):] == entry


def _apply_tool_filter(
    tools: list[BaseTool],
    server_name: str,
    server_config: dict[str, Any],
) -> list[BaseTool]:
    """Filter a server's loaded tools by ``allowedTools`` / ``disabledTools``.

    Entries may be literal tool names or fnmatch-style glob patterns.
    Unmatched entries are logged (but not errors) for stale config entries.
    """
    allowed: list[str] | None = server_config.get("allowedTools")
    disabled: list[str] | None = server_config.get("disabledTools")
    entries: list[str] | None = allowed if allowed is not None else disabled
    if entries is None:
        return tools

    # Rebind so closures capture the narrowed `list[str]` type
    filter_entries: list[str] = entries

    prefix = f"{server_name}_"
    field_name = "allowedTools" if allowed is not None else "disabledTools"

    def _any_entry_matches(tool_name: str) -> bool:
        return any(_entry_matches_tool(e, tool_name, prefix) for e in filter_entries)

    # Warn about filter entries that match no loaded tool
    missing = [
        e for e in filter_entries
        if not any(_entry_matches_tool(e, t.name, prefix) for t in tools)
    ]
    if missing:
        logger.warning(
            f"MCP server '{server_name}' {field_name} entries matched no tools: {', '.join(missing)}",
        )

    if allowed is not None:
        return [t for t in tools if _any_entry_matches(t.name)]
    return [t for t in tools if not _any_entry_matches(t.name)]


# ===========================================================================
# JSON error diagnostics (adapted from dcode)
# ===========================================================================

def _json_error_hint(exc: json.JSONDecodeError) -> str | None:
    """Return an actionable hint for common JSON mistakes."""
    msg = exc.msg.lower()
    if "trailing comma" in msg:
        return (
            "Hint: JSON does not allow trailing commas. Remove the comma "
            "before the closing '}' or ']'."
        )
    if "expecting property name" in msg:
        return (
            "Hint: check for trailing commas, a missing key, or an unquoted "
            "property name near this position."
        )
    if "expecting value" in msg:
        return (
            "Hint: check for a missing value, an extra comma, or unquoted text "
            "near this position."
        )
    return None


def _json_error_snippet(doc: str, lineno: int, colno: int) -> str | None:
    """Build a caret snippet pointing at a JSON error location."""
    lines = doc.splitlines()
    if lineno < 1 or lineno > len(lines):
        return None
    source = lines[lineno - 1].rstrip()
    if not source:
        return None
    caret_col = max(0, min(colno - 1, len(source)))
    return f"    {source}\n    {' ' * caret_col}^"


def load_mcp_config_json(config_path: str) -> dict[str, Any]:
    """Load MCP configuration JSON with enhanced error diagnostics.

    Adapted from dcode's ``_load_mcp_config_json()``.
    """
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"MCP config file not found: {config_path}")

    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        parts = [f"Invalid JSON in MCP config file: {exc.msg}"]
        hint = _json_error_hint(exc)
        if hint is not None:
            parts.append(hint)
        snippet = _json_error_snippet(exc.doc, exc.lineno, exc.colno)
        if snippet is not None:
            parts.append(snippet)
        error_msg = "\n".join(parts)
        raise MCPConfigError(error_msg) from exc


# ===========================================================================
# Internal helpers (preserved from original)
# ===========================================================================

def _resolve_auth_headers(server_def: dict[str, Any], config: Any = None) -> dict[str, str]:
    """Build HTTP headers including Bearer token from env-var if configured."""
    headers: dict[str, str] = dict(server_def.get("headers") or {})
    env_var: str | None = server_def.get("auth_token_env_var")
    if env_var:
        token = None
        if config and hasattr(config, env_var):
            token = getattr(config, env_var)
        if not token:
            token = os.getenv(env_var)

        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            logger.warning(
                "Auth token env var not set",
                extra={"env_var": env_var, "server": server_def.get("name")},
            )
    return headers


def _build_server_configs(
    config: "Config",
    server_filter: list[str] | None,
) -> dict[str, dict[str, Any]]:
    """Transform ``Config.mcp_config`` into connection configs.

    Also stores the original server definition for each entry so that
    tool filtering and pre-flight checks can access the full config.
    """
    mcp = config.get_mcp_config()
    default_transport: str = mcp.get("default_transport", "sse")
    servers: dict[str, dict[str, Any]] = {}

    for sdef in mcp.get("servers", []):
        name = sdef.get("name")
        if not name:
            logger.warning("Skipping MCP server entry without a 'name' key")
            continue
        if server_filter and name not in server_filter:
            continue
        if sdef.get("disabled", False):
            logger.debug("Skipping disabled server", extra={"server": name})
            continue

        transport = sdef.get("transport", default_transport)

        if transport in ("sse", "http"):
            url = sdef.get("url")
            if not url:
                logger.warning(f"{transport} transport requires 'url'", extra={"server": name})
                continue
            entry: dict[str, Any] = {"url": url, "transport": transport}
            headers = _resolve_auth_headers(sdef, config)
            if headers:
                entry["headers"] = headers
            # Store original sdef for tool filtering access
            entry["_server_def"] = sdef
            servers[name] = entry

        elif transport == "stdio":
            server_env = sdef.get("env") or {}
            merged_env = {
                **os.environ,
                **(server_env if isinstance(server_env, dict) else {}),
                "MCP_TRANSPORT": "stdio",
            }
            entry = {
                "command": sdef.get("command", "python"),
                "args": sdef.get("args", []),
                "transport": "stdio",
                "env": merged_env,
                "_server_def": sdef,
            }
            servers[name] = entry
        else:
            logger.warning("Unknown transport", extra={"server": name, "transport": transport})

    return servers


# Gemini schema sanitization (our unique strength — dcode doesn't have this)
_GEMINI_SUPPORTED_SCHEMA_KEYS = frozenset({
    "type", "type_", "description", "enum", "format", "items",
    "properties", "required", "nullable", "anyOf", "default",
    "minimum", "maximum", "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "title",
})


def _sanitize_schema(schema: Any) -> Any:
    """Recursively strip JSON-Schema keys that the Gemini API doesn't support."""
    if not isinstance(schema, dict):
        if isinstance(schema, list):
            return [_sanitize_schema(item) for item in schema]
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                field_name: _sanitize_schema(field_schema)
                for field_name, field_schema in value.items()
            }
        elif key in ("items", "anyOf") or isinstance(value, (dict, list)):
            cleaned[key] = _sanitize_schema(value)
        else:
            cleaned[key] = value

    return cleaned


def coerce_tool_arguments(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerces stringified JSON/YAML arguments into native Python objects."""
    if not arguments:
        return arguments

    for key, value in list(arguments.items()):
        if isinstance(value, str):
            stripped = value.strip()
            if (stripped.startswith('{') and stripped.endswith('}')) or \
               (stripped.startswith('[') and stripped.endswith(']')):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, (dict, list)):
                        arguments[key] = parsed
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                try:
                    parsed = yaml.safe_load(stripped)
                    if isinstance(parsed, (dict, list)):
                        arguments[key] = parsed
                except yaml.YAMLError:
                    pass

    return arguments


def _wrap_tool(
    original: BaseTool,
    server_name: str,
    execute_fn: Callable[..., Any],
) -> BaseTool:
    """Wrap an MCP tool with error handling and schema sanitization."""
    tool_name = original.name

    async def _proxy(**kwargs: Any) -> Any:
        return await execute_fn(server_name, tool_name, kwargs)

    args_schema = getattr(original, "args_schema", None)
    if isinstance(args_schema, dict):
        args_schema = _sanitize_schema(args_schema)

    tool_kwargs: Dict[str, Any] = {
        "name": tool_name,
        "description": original.description or "",
        "coroutine": _proxy,
        "func": lambda **_: None,
        "handle_tool_error": True,
    }
    if args_schema is not None:
        tool_kwargs["args_schema"] = args_schema
    wrapped = StructuredTool(**tool_kwargs)  # type: ignore[arg-type]
    return wrapped


# ===========================================================================
# MCP Server Info — status metadata for prompt injection
# ===========================================================================

@dataclass
class MCPServerInfo:
    """Status metadata for a discovered MCP server.

    Adapted from dcode — gathered during tool loading and injected into
    the system prompt so the LLM knows which tools are active, errored,
    or require authentication.
    """
    name: str
    transport: str
    status: str = "connected"  # "connected" | "error" | "unauthenticated"
    error: str | None = None
    tool_count: int = 0


# ===========================================================================
# Public API: MCPClient
# ===========================================================================

class MCPClient:
    """Async MCP client with lazy session management and transient error recovery.

    Enhanced lifecycle::

        client = MCPClient(config)
        async with client.connect():
            tools = client.get_tools()
            # sessions created lazily on first tool call
        # all sessions cleaned up here

    Key improvements over the original:
      - Lazy session management via ``MCPSessionManager``
      - Pre-flight health checks (command-on-PATH, HTTP HEAD)
      - Retry-once on transient errors (broken pipes, EOF)
      - ``allowedTools`` / ``disabledTools`` filtering
      - Schema-aware argument normalization
      - Paginated tool discovery
    """

    __slots__ = (
        "_config",
        "_server_filter",
        "_server_configs",
        "_session_manager",
        "_tools",
        "_tool_map",
        "_tool_schemas",
        "_tool_timeout",
        "_server_infos",
    )

    def __init__(
        self,
        config: Optional["Config"] = None,
        server_filter: Optional[List[str]] = None,
    ) -> None:
        if config is None:
            from k8s_autopilot.config.config import Config
            config = Config()

        self._config = config
        self._server_filter = server_filter
        self._server_configs = _build_server_configs(config, server_filter)
        self._tool_timeout: float = float(
            config.get("MCP_TIMEOUT_TOTAL", _DEFAULT_TOOL_TIMEOUT)
        )

        # Session state
        self._session_manager: Optional[MCPSessionManager] = None
        self._tools: list[BaseTool] = []
        self._tool_map: dict[str, BaseTool] = {}
        self._tool_schemas: dict[str, Any] = {}  # tool_name → inputSchema
        self._server_infos: list[MCPServerInfo] = []

        logger.info(
            "MCPClient created",
            extra={"servers": list(self._server_configs.keys())},
        )

    # ── Connection lifecycle ─────────────────────────────────────────────

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["MCPClient"]:
        """Discover tools from all MCP servers and prepare lazy sessions.

        Discovery uses throwaway sessions. Live sessions are created
        lazily on first tool call via ``MCPSessionManager``.
        """
        if not self._server_configs:
            logger.warning("No MCP servers configured — nothing to connect to")
            yield self
            return

        # Create session manager for lazy runtime connections
        self._session_manager = MCPSessionManager(
            connections=self._server_configs,
        )

        all_tools: list[BaseTool] = []
        server_infos: list[MCPServerInfo] = []

        for name, conn_config in self._server_configs.items():
            sdef = conn_config.get("_server_def", conn_config)
            transport = sdef.get("transport", "sse")

            # ── Pre-flight health checks ──────────────────────────────
            try:
                if transport == "stdio":
                    _check_stdio_server(name, sdef)
                elif transport in ("sse", "http"):
                    await _check_remote_server(name, sdef)
            except MCPClientError as exc:
                logger.warning(
                    f"MCP server '{name}' skipped: pre-flight failed: {exc}",
                )
                server_infos.append(MCPServerInfo(
                    name=name, transport=transport,
                    status="error", error=str(exc),
                ))
                continue

            # ── Tool discovery (throwaway session) ────────────────────
            try:
                from typing import cast
                clean_connection = {k: v for k, v in conn_config.items() if k != "_server_def"}
                from langchain_mcp_adapters.client import MultiServerMCPClient
                client = MultiServerMCPClient(cast(Any, {name: clean_connection}))
                async with client.session(name) as session:
                    try:
                        await session.set_logging_level("warning")
                    except Exception:
                        pass

                    from langchain_mcp_adapters.tools import load_mcp_tools
                    try:
                        server_tools = await _discover_tools(session)
                    except Exception:
                        server_tools = await load_mcp_tools(session)

                # Apply tool filtering
                wrapped_tools = [_wrap_tool(t, name, self.execute_tool) for t in server_tools]
                filtered_tools = _apply_tool_filter(wrapped_tools, name, sdef)

                # Store input schemas for argument normalization
                for t in server_tools:
                    schema = getattr(t, "inputSchema", None)
                    if schema:
                        self._tool_schemas[t.name] = schema

                all_tools.extend(filtered_tools)
                server_infos.append(MCPServerInfo(
                    name=name, transport=transport,
                    status="connected",
                    tool_count=len(filtered_tools),
                ))
                logger.info(
                    "Connected to MCP server",
                    extra={
                        "server": name,
                        "tools": len(filtered_tools),
                        "filtered_from": len(server_tools),
                    },
                )

            except Exception as conn_err:
                # ── Detect auth failures ──────────────────────────────
                err_str = str(conn_err).lower()
                if any(kw in err_str for kw in ("401", "403", "unauthorized", "forbidden")):
                    auth_var = sdef.get("auth_token_env_var", "")
                    logger.error(
                        f"MCP server '{name}' authentication failed",
                        extra={"server": name, "auth_env_var": auth_var},
                    )
                    server_infos.append(MCPServerInfo(
                        name=name, transport=transport,
                        status="unauthenticated",
                        error=f"Authentication failed — check '{auth_var}'",
                    ))
                else:
                    logger.error(
                        f"Failed to connect to MCP server '{name}'",
                        extra={"server": name, "error": str(conn_err)},
                    )
                    server_infos.append(MCPServerInfo(
                        name=name, transport=transport,
                        status="error", error=str(conn_err),
                    ))

        self._tools = all_tools
        self._tool_map = {t.name: t for t in all_tools}
        self._server_infos = server_infos
        logger.info("All MCP tools loaded", extra={"total": len(all_tools)})

        try:
            yield self
        finally:
            await self._close()

    async def _close(self) -> None:
        """Tear down all sessions and reset internal state."""
        if self._session_manager:
            await self._session_manager.cleanup()
            self._session_manager = None
        self._tools.clear()
        self._tool_map.clear()
        self._tool_schemas.clear()
        self._server_infos.clear()
        logger.info("MCPClient closed — all sessions terminated")

    # ── Tool access ──────────────────────────────────────────────────────

    def get_tools(self) -> List[BaseTool]:
        """Return all loaded LangChain tools (across all servers)."""
        return list(self._tools)

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Look up a single tool by name. Returns ``None`` if not found."""
        return self._tool_map.get(name)

    def get_server_infos(self) -> list[MCPServerInfo]:
        """Return status metadata for all discovered servers."""
        return list(self._server_infos)

    # ── Tool execution with transient error recovery ─────────────────────

    async def execute_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        """Execute a tool with transient error recovery.

        Enhanced with dcode patterns:
          - Schema-aware argument normalization
          - Transient error detection (broken pipe, EOF)
          - Retry-once: invalidate dead session, reconnect, retry
        """
        if not self._session_manager:
            raise MCPClientError("MCPClient not connected — use 'async with client.connect()'")

        logger.debug(
            "Executing MCP tool",
            extra={"server": server_name, "tool": tool_name},
        )

        # Apply both our YAML coercion and dcode's schema normalization
        arguments = coerce_tool_arguments(arguments) or {}
        input_schema = self._tool_schemas.get(tool_name)
        if input_schema:
            arguments = _normalize_mcp_arguments(arguments, input_schema)

        session = await self._session_manager.get_session(server_name)

        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=arguments),
                timeout=self._tool_timeout,
            )
            return self._unwrap_result(result)

        except asyncio.TimeoutError:
            logger.error(
                "Tool execution timed out",
                extra={
                    "server": server_name,
                    "tool": tool_name,
                    "timeout_s": self._tool_timeout,
                },
            )
            raise MCPClientError(
                f"Tool '{tool_name}' on '{server_name}' timed out "
                f"after {self._tool_timeout}s"
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            # ── Transient error → retry once (dcode pattern) ──────────
            if not _is_transient_session_error(exc):
                logger.error(
                    "Tool execution failed",
                    extra={"server": server_name, "tool": tool_name, "error": str(exc)},
                )
                raise MCPClientError(f"Tool execution failed: {exc}") from exc

            logger.info(
                f"MCP session for {server_name!r} appears dead "
                f"({type(exc).__name__}: {exc}); invalidating and retrying once",
            )
            await self._session_manager.invalidate(
                server_name, expected_session=session,
            )

            # Retry with a fresh session
            retry_session = await self._session_manager.get_session(server_name)
            try:
                result = await asyncio.wait_for(
                    retry_session.call_tool(tool_name, arguments=arguments),
                    timeout=self._tool_timeout,
                )
                return self._unwrap_result(result)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as retry_exc:
                try:
                    raise MCPClientError(
                        f"Tool '{tool_name}' failed after one retry on "
                        f"'{server_name}': {retry_exc}"
                    ) from retry_exc
                finally:
                    try:
                        await self._session_manager.invalidate(
                            server_name, expected_session=retry_session,
                        )
                    except Exception:
                        logger.warning(
                            f"Failed to invalidate retry session for {server_name!r}",
                            exc_info=True,
                        )

    @staticmethod
    def _unwrap_result(result: Any) -> Any:
        """Unwrap single-text MCP results for convenience."""
        if (
            result.content
            and len(result.content) == 1
            and hasattr(result.content[0], "text")
        ):
            return result.content[0].text
        return result.content

    # ── Resources ────────────────────────────────────────────────────────

    async def list_resources(
        self,
        server_name: Optional[str] = None,
    ) -> List[Any]:
        """List available resources on a server."""
        session = await self._get_session_for(server_name)
        try:
            result = await session.list_resources()
            return result.resources
        except Exception as exc:
            raise MCPClientError(f"Failed to list resources: {exc}") from exc

    async def read_resource(
        self,
        uri: str,
        server_name: Optional[str] = None,
    ) -> Any:
        """Read a single resource by URI."""
        session = await self._get_session_for(server_name)
        try:
            from pydantic import AnyUrl
            return await session.read_resource(AnyUrl(uri))
        except Exception as exc:
            raise MCPClientError(f"Failed to read resource '{uri}': {exc}") from exc

    # ── Prompts ──────────────────────────────────────────────────────────

    async def get_prompt(
        self,
        prompt_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        server_name: Optional[str] = None,
    ) -> List[BaseMessage]:
        """Fetch a prompt template and return it as LangChain messages."""
        session = await self._get_session_for(server_name)
        try:
            result = await session.get_prompt(
                prompt_name, arguments=arguments or {}
            )
            return self._convert_messages(result.messages)
        except Exception as exc:
            raise MCPClientError(
                f"Failed to fetch prompt '{prompt_name}': {exc}"
            ) from exc

    # ── Private helpers ──────────────────────────────────────────────────

    async def _get_session_for(self, server_name: str | None) -> "ClientSession":
        """Get a session by name or the first configured server."""
        if not self._session_manager:
            raise MCPClientError("MCPClient not connected")
        if server_name:
            return await self._session_manager.get_session(server_name)
        names = self._session_manager.server_names
        if not names:
            raise MCPClientError("No MCP servers connected")
        return await self._session_manager.get_session(names[0])

    @staticmethod
    def _convert_messages(raw_messages: list[Any]) -> list[BaseMessage]:
        """Convert MCP prompt messages → LangChain BaseMessage list."""
        result: list[BaseMessage] = []
        for msg in raw_messages:
            if hasattr(msg.content, "text"):
                text = msg.content.text
            elif isinstance(msg.content, list):
                text = "\n".join(
                    c.text for c in msg.content if hasattr(c, "text")
                )
            else:
                text = str(msg.content)

            if msg.role == "user":
                result.append(HumanMessage(content=text))
            elif msg.role == "assistant":
                result.append(AIMessage(content=text))
            else:
                result.append(SystemMessage(content=text))
        return result


# ===========================================================================
# Convenience factory
# ===========================================================================

@asynccontextmanager
async def create_mcp_client(
    config: Optional["Config"] = None,
    server_filter: Optional[List[str]] = None,
) -> AsyncIterator[MCPClient]:
    """One-liner factory::

        async with create_mcp_client(config) as client:
            tools = client.get_tools()
    """
    client = MCPClient(config, server_filter)
    async with client.connect():
        yield client