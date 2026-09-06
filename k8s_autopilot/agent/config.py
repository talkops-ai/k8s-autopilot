"""Agent context and configuration schemas for K8s Autopilot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    """Runtime context passed through the agent lifecycle."""

    model: str = "gemini-3.7-flash"
    thread_id: str = ""
    turn_id: str = ""
    approval_mode: str = "manual"  # yolo | auto | manual
    working_dir: str = "."
    interactive: bool = True
    auto_approve: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CLIContextSchema:
    """Standard context schema for the K8s Autopilot LangGraph execution graph."""

    model: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    model_context_limit: int | None = None
    approval_mode: str = "manual"
    auto_approve: bool = False
    approval_mode_key: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    offload_tool_call_id: str | None = None
    hooks_snapshot_id: str | None = None
    hooks_server_events: list[str] = field(default_factory=list)
    prompt_id: str | None = None
    reasoning_effort: str | None = None


AgentContextSchema = CLIContextSchema

__all__ = ["AgentContext", "AgentContextSchema", "CLIContextSchema"]
