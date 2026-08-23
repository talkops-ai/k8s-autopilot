"""
K8s Autopilot Agent — Pydantic Config Schema.

Safety-gate validation layer that mirrors ``DefaultConfig`` with type
constraints, range checks, and provider validation.

Design philosophy (from dcode):
  - **Never crash** — validation errors log warnings and fall back to defaults
  - ``extra="ignore"`` for forward compatibility (unknown keys are silently skipped)
  - Validators use ``mode="before"`` to coerce raw env-var strings before
    Pydantic's own type checking kicks in

Usage::

    from k8s_autopilot.config.schema import validate_config

    # Inside Config.__init__() after resolving all values:
    warnings = validate_config(resolved_store)
    # warnings is a list[str] of any issues found; empty if all OK
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = AgentLogger("ConfigSchema")


# ---------------------------------------------------------------------------
# MCP Server Entry schema
# ---------------------------------------------------------------------------

class MCPServerEntrySchema(BaseModel):
    """Schema for a single MCP server configuration entry."""

    model_config = ConfigDict(extra="allow")

    name: str
    transport: str = "stdio"
    command: Optional[str] = None
    url: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        KNOWN = {"stdio", "sse", "http", "streamable_http", "streamable-http"}
        if v not in KNOWN:
            logger.warning(f"Unknown MCP transport: {v!r}")
        return v

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerEntrySchema":
        """Ensure stdio entries have command and http entries have url."""
        if self.transport == "stdio" and not self.command:
            logger.warning(
                f"MCP server {self.name!r}: stdio transport requires 'command'"
            )
        if self.transport in ("http", "sse") and not self.url:
            logger.warning(
                f"MCP server {self.name!r}: {self.transport} transport requires 'url'"
            )
        return self


# ---------------------------------------------------------------------------
# Main Config Schema
# ---------------------------------------------------------------------------

class ConfigSchema(BaseModel):
    """Pydantic validation schema for Config — safety gate, not enforcer.

    Mirrors ``DefaultConfig`` with type constraints and range validations.
    Uses ``extra="ignore"`` for forward compatibility: unknown keys
    (e.g. future additions, API key env vars) are silently skipped.
    """

    model_config = ConfigDict(extra="ignore")

    # ── LLM: Standard tier ────────────────────────────────────────────────
    LLM_PROVIDER: str = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = Field(ge=0.0, le=2.0, default=0.0)
    LLM_MAX_TOKENS: int = Field(gt=0, default=15000)
    LLM_THINKING_ENABLED: bool = False
    LLM_THINKING_BUDGET: Optional[int] = None
    LLM_REASONING_EFFORT: Optional[str] = None

    # ── LLM: Higher tier ──────────────────────────────────────────────────
    LLM_HIGHER_PROVIDER: str = "openai"
    LLM_HIGHER_MODEL: str = "gpt-5-mini"
    LLM_HIGHER_TEMPERATURE: float = Field(ge=0.0, le=2.0, default=0.0)
    LLM_HIGHER_MAX_TOKENS: int = Field(gt=0, default=15000)
    LLM_HIGHER_THINKING_ENABLED: bool = False
    LLM_HIGHER_THINKING_BUDGET: Optional[int] = None

    # ── LLM: DeepAgent tier ──────────────────────────────────────────────
    LLM_DEEPAGENT_PROVIDER: str = "openai"
    LLM_DEEPAGENT_MODEL: str = "o4-mini"
    LLM_DEEPAGENT_TEMPERATURE: float = Field(ge=0.0, le=2.0, default=1.0)
    LLM_DEEPAGENT_MAX_TOKENS: int = Field(gt=0, default=25000)
    LLM_DEEPAGENT_THINKING_ENABLED: bool = False
    LLM_DEEPAGENT_THINKING_BUDGET: Optional[int] = None
    LLM_DEEPAGENT_REASONING_EFFORT: Optional[str] = None

    # ── PostgreSQL ────────────────────────────────────────────────────────
    POSTGRES_URI: Optional[str] = None

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── A2A Server ────────────────────────────────────────────────────────
    A2A_SERVER_HOST: str = "localhost"
    A2A_SERVER_PORT: int = Field(gt=0, le=65535, default=10102)

    # ── LangGraph ─────────────────────────────────────────────────────────
    RECURSION_LIMIT: int = Field(gt=0, default=50)

    # ── Execution Sandbox ─────────────────────────────────────────────────
    K8S_SANDBOX_PROVIDER: str = "local"

    # ── Supervisor Context Engineering ────────────────────────────────────
    SUPERVISOR_SUMMARIZATION_TRIGGER_TOKENS: int = Field(gt=0, default=4000)
    SUPERVISOR_SUMMARIZATION_KEEP_MESSAGES: int = Field(gt=0, default=6)
    SUPERVISOR_MODEL_CALL_LIMIT: int = Field(gt=0, default=15)

    # ── MCP Servers ───────────────────────────────────────────────────────
    MCP_SERVERS: List[MCPServerEntrySchema] = Field(default_factory=list)
    MCP_TIMEOUT_TOTAL: float = Field(gt=0, default=600.0)
    MCP_TIMEOUT_CONNECT: float = Field(gt=0, default=300.0)

    # ── Conversation Compaction ───────────────────────────────────────────
    COMPACTION_TOKEN_BUDGET: int = Field(gt=0, default=100_000)
    COMPACTION_KEEP_MESSAGES: int = Field(gt=0, default=6)
    COMPACTION_SUMMARY_MODEL: Optional[str] = None

    # ── Rubric Evaluation ─────────────────────────────────────────────────
    RUBRIC_GRADER_MODEL: Optional[str] = None
    RUBRIC_MAX_ITERATIONS: int = Field(gt=0, default=3)

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("LLM_PROVIDER", "LLM_HIGHER_PROVIDER", "LLM_DEEPAGENT_PROVIDER")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        KNOWN = {
            "openai", "anthropic", "google_genai", "gemini",
            "fireworks", "ollama", "openrouter", "aws",
            "bedrock", "aws_bedrock", "bedrock_converse",
            "azure_openai",
        }
        if v not in KNOWN:
            logger.warning(f"Unknown LLM provider: {v!r}")
        return v

    @field_validator("LLM_REASONING_EFFORT", "LLM_DEEPAGENT_REASONING_EFFORT")
    @classmethod
    def validate_reasoning_effort(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        KNOWN = {"low", "medium", "high"}
        if v.lower() not in KNOWN:
            logger.warning(
                f"Unknown reasoning effort: {v!r}. Expected one of {KNOWN}"
            )
        return v.lower()

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        KNOWN = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in KNOWN:
            logger.warning(f"Unknown log level: {v!r}")
        return v.upper()

    @field_validator("K8S_SANDBOX_PROVIDER")
    @classmethod
    def validate_sandbox_provider(cls, v: str) -> str:
        KNOWN = {"local", "daytona", "agentcore", "langsmith", "modal", "runloop"}
        if v not in KNOWN:
            logger.warning(f"Unknown sandbox provider: {v!r}")
        return v

    @field_validator("MCP_SERVERS", mode="before")
    @classmethod
    def coerce_mcp_servers(cls, v: Any) -> Any:
        """Handle MCP_SERVERS being passed as a JSON string from env vars."""
        if isinstance(v, str):
            import json
            try:
                return json.loads(v) if v.strip() else []
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Failed to parse MCP_SERVERS JSON string")
                return []
        return v


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_config(store: Dict[str, Any]) -> List[str]:
    """Validate a resolved config store via Pydantic.

    This is a **safety gate** — it logs warnings and returns a list of
    validation issues but never raises. The caller should continue with
    the unmodified store; Pydantic validation is advisory only.

    Args:
        store: The resolved config dict from ``Config.__init__()``.

    Returns:
        A list of human-readable validation warning strings.
        Empty list means no issues found.
    """
    try:
        ConfigSchema(**store)
        return []
    except Exception as e:
        # Extract individual error messages from Pydantic ValidationError
        warnings_list: List[str] = []
        if hasattr(e, "errors"):
            for err in e.errors():  # type: ignore[union-attr]
                loc = " → ".join(str(l) for l in err.get("loc", []))
                msg = err.get("msg", str(err))
                warnings_list.append(f"Config[{loc}]: {msg}")
        else:
            warnings_list.append(str(e))

        for w in warnings_list:
            logger.warning(f"Config validation: {w}")

        return warnings_list
