"""
Project-level .mcp.json discovery.

Scans project-level ``.mcp.json`` files for additional MCP server definitions
that supplement the ``DefaultConfig.MCP_SERVERS`` list.

Design decisions:
  - NO Claude Desktop scanning (user confirmed this is k8s-autopilot specific)
  - Only project-level ``.mcp.json`` files are scanned
  - Discovered servers merge with defaults (project overrides by name)
  - Trust-gated: untrusted configs are logged but not loaded

Reference: dcode/code/mcp_tools.py (MCPConfigError, discovery logic)
"""

from __future__ import annotations

import json
from k8s_autopilot.utils.logger import AgentLogger
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from k8s_autopilot.core.mcp.trust import MCPTrustStore

logger = AgentLogger("MCPDiscovery")


@dataclass
class MCPServerSpec:
    """Parsed MCP server specification from a .mcp.json file.

    Represents a single server entry with transport, connection, and
    environment details. Can be converted to the dict format expected
    by ``DefaultConfig.MCP_SERVERS``.
    """

    name: str
    transport: str = "stdio"  # stdio | http | sse
    command: Optional[str] = None
    url: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    disabled: bool = False

    def to_config_dict(self) -> Dict[str, Any]:
        """Convert to the dict format used by Config.MCP_SERVERS."""
        d: Dict[str, Any] = {
            "name": self.name,
            "transport": self.transport,
            "disabled": self.disabled,
        }
        if self.command:
            d["command"] = self.command
        if self.url:
            d["url"] = self.url
        if self.args:
            d["args"] = self.args
        if self.env:
            d["env"] = self.env
        if self.headers:
            d["headers"] = self.headers
        return d


class MCPDiscovery:
    """Scan project-level .mcp.json for additional MCP servers.

    Discovery paths (relative to project root):
      - ``.mcp.json``
      - ``.k8s-autopilot/.mcp.json``

    The ``.mcp.json`` format follows the Claude Desktop convention:

    .. code-block:: json

        {
          "mcpServers": {
            "server-name": {
              "command": "binary-name",
              "args": ["--flag"],
              "env": {"KEY": "VALUE"}
            }
          }
        }

    Or for HTTP servers:

    .. code-block:: json

        {
          "mcpServers": {
            "server-name": {
              "url": "http://host:port/mcp",
              "transport": "http"
            }
          }
        }
    """

    SEARCH_PATHS = [
        Path(".mcp.json"),
        Path(".k8s-autopilot/.mcp.json"),
    ]

    def __init__(self, trust_store: Optional["MCPTrustStore"] = None) -> None:
        self._trust_store = trust_store

    def discover(
        self,
        project_root: Optional[Path] = None,
    ) -> List[MCPServerSpec]:
        """Scan for project-level .mcp.json files and parse server specs.

        Args:
            project_root: Root directory to scan from. Defaults to cwd.

        Returns:
            List of discovered MCP server specifications.
        """
        root = project_root or Path.cwd()
        discovered: List[MCPServerSpec] = []

        for rel_path in self.SEARCH_PATHS:
            config_path = root / rel_path
            if not config_path.exists():
                continue

            # Trust gate
            if self._trust_store and not self._trust_store.is_trusted(config_path):
                logger.warning(f"Untrusted MCP config at {config_path} — run `k8s-autopilot mcp trust {config_path}` "
                    "to approve")
                continue

            try:
                specs = self._parse_config(config_path)
                discovered.extend(specs)
                logger.info(f"Discovered {len(specs):d} MCP server(s) from {config_path}")
            except Exception as e:
                logger.error(f"Failed to parse MCP config {config_path}: {e}", exc_info=True)

        return discovered

    def _parse_config(self, config_path: Path) -> List[MCPServerSpec]:
        """Parse a single .mcp.json file into MCPServerSpec list."""
        raw = json.loads(config_path.read_text(encoding="utf-8"))

        # Support both "mcpServers" (Claude Desktop) and "servers" (custom) keys
        servers_dict = raw.get("mcpServers") or raw.get("servers") or {}

        specs: List[MCPServerSpec] = []
        for name, entry in servers_dict.items():
            if not isinstance(entry, dict):
                logger.warning(f"Skipping non-dict MCP server entry {name!r} in {config_path}")
                continue

            spec = MCPServerSpec(
                name=name,
                transport=entry.get("transport", "stdio"),
                command=entry.get("command"),
                url=entry.get("url"),
                args=entry.get("args", []),
                env=entry.get("env", {}),
                headers=entry.get("headers", {}),
                disabled=entry.get("disabled", False),
            )
            specs.append(spec)

        return specs

    def merge_with_defaults(
        self,
        defaults: List[Dict[str, Any]],
        discovered: List[MCPServerSpec],
    ) -> List[Dict[str, Any]]:
        """Merge discovered servers with default config servers.

        Project-level configs override defaults by name. New servers are
        appended. Disabled servers are filtered out.

        Args:
            defaults: The ``MCP_SERVERS`` list from ``DefaultConfig``.
            discovered: List of discovered ``MCPServerSpec`` instances.

        Returns:
            Merged server list in ``DefaultConfig.MCP_SERVERS`` dict format.
        """
        # Index defaults by name
        merged: Dict[str, Dict[str, Any]] = {}
        for server in defaults:
            name = server.get("name", "")
            if name:
                merged[name] = dict(server)

        # Override/add discovered servers
        for spec in discovered:
            if spec.disabled:
                # Remove disabled servers from the merged set
                merged.pop(spec.name, None)
                logger.info(f"MCP server {spec.name!r} disabled by project config")
                continue
            merged[spec.name] = spec.to_config_dict()

        return list(merged.values())
