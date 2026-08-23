"""
Pydantic schema for subagent ``config.yaml`` files.

Each subagent under ``plugins/{domain}/agents/{name}/config.yaml`` is
validated against this schema at load time.  The schema is the **single
source of truth** for everything the registry and middleware builder
need to create a subagent — identity, MCP servers, tools, HITL gates,
skills, memory, middleware, and prompt composition.

Usage::

    from k8s_autopilot.core.agents.agent_config import load_agent_config

    config = load_agent_config(Path("plugins/helm-operator/agents/helm-operation/config.yaml"))
    print(config.name)           # "helm-operation"
    print(config.hitl_tools)     # ["helm_install_chart", ...]
    print(config.middleware.ptc_allowlist)  # ["read_mcp_resource", ...]

Design:
    - Pydantic ``BaseModel`` with ``extra="forbid"`` — typos in YAML are
      caught at startup, not silently ignored.
    - ``hitl_tools`` (simple list) and ``hitl_config`` (per-tool decisions)
      are mutually composable — ``hitl_config`` overrides ``hitl_tools``
      for tools that appear in both.
    - ``prompt_composer`` is an optional Python function name resolved at
      runtime by the middleware builder.  When absent, ``prompts/system.md``
      is used directly.

Reference: k8s_autopilot/config/schema.py (MCPServerEntrySchema pattern)
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("AgentConfig")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class HITLDecision(str, Enum):
    """Allowed decision types for HITL gates."""

    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# Nested schemas
# ---------------------------------------------------------------------------

class HITLToolConfig(BaseModel):
    """Fine-grained HITL configuration for a single tool.

    Use this when different tools need different ``allowed_decisions``
    (e.g., ``create_application`` allows edit, ``delete_application``
    does not).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(description="MCP tool name that requires human approval.")
    allowed_decisions: list[HITLDecision] = Field(
        default=[HITLDecision.APPROVE, HITLDecision.REJECT],
        description="Decision types the reviewer can make for this tool.",
    )


class MiddlewareConfig(BaseModel):
    """Middleware configuration for the agent.

    Centralises settings that were previously scattered across
    ``_get_domain_ptc_allowlists()`` and per-domain constants.
    """

    model_config = ConfigDict(extra="forbid")

    ptc_allowlist: list[str] = Field(
        default_factory=lambda: ["read_mcp_resource", "read_file", "ls"],
        description=(
            "Tools allowed for programmatic tool calling (eval/code interpreter). "
            "Only read-only, side-effect-free tools should be listed here."
        ),
    )


# ---------------------------------------------------------------------------
# Main schema
# ---------------------------------------------------------------------------

from typing import Any

# ---------------------------------------------------------------------------
# Nested schemas
# ---------------------------------------------------------------------------

class ToolSpec(BaseModel):
    """Configuration for an extra tool."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="The name of the extra tool.")
    config: dict[str, Any] = Field(default_factory=dict, description="Custom parameters for the extra tool.")


class MCPSpec(BaseModel):
    """Configuration for an MCP server connection."""
    model_config = ConfigDict(extra="forbid")
    server_name: str = Field(description="The name of the MCP server.")
    tool_name_prefix: bool = Field(default=True, description="Whether to prefix tool names with the server name.")


class ToolsConfig(BaseModel):
    """Configuration for the tools exposed to the agent."""
    model_config = ConfigDict(extra="forbid")
    mcp: list[MCPSpec] = Field(default_factory=list, description="MCP servers to connect.")
    extra: list[ToolSpec] = Field(default_factory=list, description="Extra tools to build.")


class MiddlewareSpec(BaseModel):
    """Configuration for a middleware class."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="The registered name of the middleware.")
    config: dict[str, Any] = Field(default_factory=dict, description="Custom configuration for the middleware.")


# ---------------------------------------------------------------------------
# Main schema
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Pydantic schema for a subagent's ``config.yaml``.

    This is the single source of truth for everything the registry
    and middleware builder need to create a subagent.
    """

    model_config = ConfigDict(extra="forbid")

    # ── Identity ──────────────────────────────────────────────────────
    name: str = Field(
        description="Unique agent identifier, used with the task tool.",
    )
    description: str = Field(
        description=(
            "What this agent does.  The coordinator uses this to decide "
            "when to delegate to this subagent."
        ),
    )
    model: Optional[str] = Field(
        default=None,
        description=(
            "Model override in 'provider:model-name' format.  "
            "Falls back to coordinator model when absent."
        ),
    )
    agent_type: Literal["react", "deep"] = Field(
        default="react",
        description="Type of the subagent. Standard flat agent (react) or nested deep coordinator (deep).",
    )
    deep_config: Optional[dict[str, Any]] = Field(
        default=None,
        description="Custom configuration for the nested deep coordinator agent.",
    )

    # ── Tools ─────────────────────────────────────────────────────────
    tools: ToolsConfig = Field(
        default_factory=ToolsConfig,
        description="Declarative tools configuration.",
    )

    # ── Prompt ────────────────────────────────────────────────────────
    prompt_composer: Optional[str] = Field(
        default=None,
        description=(
            "Python function name that dynamically composes the system prompt.  "
            "When set, ``prompts/system.md`` is used as fallback only.  "
            "The function is resolved from the prompt composer registry."
        ),
    )

    # ── Middleware ─────────────────────────────────────────────────────
    middleware: list[MiddlewareSpec] = Field(
        default_factory=list,
        description="Declarative middleware configuration list.",
    )

    # ── Validators ────────────────────────────────────────────────────

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Ensure name is non-empty after stripping whitespace."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Agent name cannot be empty")
        return stripped

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: str) -> str:
        """Ensure description is non-empty."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Agent description cannot be empty")
        return stripped

    # ── Computed properties ───────────────────────────────────────────

    @property
    def has_hitl(self) -> bool:
        """Whether this agent has any HITL-gated tools."""
        return any(mw.name == "human_in_the_loop" for mw in self.middleware)

    @property
    def has_mcp(self) -> bool:
        """Whether this agent needs MCP server connections."""
        return bool(self.tools.mcp)



# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_agent_config(config_path: Path) -> AgentConfig:
    """Load and validate an agent's ``config.yaml``.

    Args:
        config_path: Absolute path to the ``config.yaml`` file.

    Returns:
        Validated ``AgentConfig`` instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        pydantic.ValidationError: If the YAML does not match the schema.
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"Expected a YAML mapping in {config_path}, got {type(raw).__name__}"
        )
    return AgentConfig.model_validate(raw)
