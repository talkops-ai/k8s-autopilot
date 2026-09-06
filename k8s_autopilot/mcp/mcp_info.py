"""Data structures for MCP server and tool metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MCPServerStatus = Literal["ok", "unauthenticated", "error", "disabled", "disconnected"]


@dataclass(frozen=True)
class MCPToolInfo:
    """Lightweight metadata about a single tool exposed by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema or {},
        }


@dataclass(frozen=True)
class MCPServerInfo:
    """Metadata about a configured MCP server, including probed tools."""

    name: str
    transport: str = "stdio"
    status: MCPServerStatus = "ok"
    error: str | None = None
    tools: tuple[MCPToolInfo, ...] = ()
    enabled: bool = True
    source: str = "project"
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    disabled_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    @property
    def is_disabled(self) -> bool:
        return not self.enabled

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    @property
    def display_target(self) -> str:
        if self.transport in ("http", "sse", "streamable_http"):
            return self.url or ""
        if self.command:
            parts = [self.command, *self.args]
            return " ".join(parts)
        return ""

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "name": self.name,
            "transport": self.transport,
            "status": self.status,
            "error": self.error,
            "url": self.url,
            "command": self.command,
            "args": list(self.args),
            "tool_count": self.tool_count,
            "tools": [t.to_dict() for t in self.tools],
            "enabled": self.enabled,
        }
        return res
