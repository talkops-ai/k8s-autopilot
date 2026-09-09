"""Typed configuration for the K8s Autopilot server."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any

SERVER_ENV_PREFIX = "K8S_AUTOPILOT_SERVER_"
DEFAULT_ASSISTANT_ID = "k8s-autopilot"


def _read_env_bool(suffix: str, *, default: bool = False) -> bool:
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return default
    return raw.lower() == "true"


def _read_env_json(suffix: str) -> Any:
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse {SERVER_ENV_PREFIX}{suffix} as JSON: {exc}. Value was: {raw[:200]!r}"
        raise ValueError(msg) from exc


def _read_env_str(suffix: str) -> str | None:
    return os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")


def _read_env_int(suffix: str, *, default: int | None = None) -> int | None:
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ServerConfig:
    """Full configuration payload for the K8s Autopilot server."""

    model: str | None = None
    model_params: dict[str, Any] | None = None
    profile_override: dict[str, Any] | None = None
    assistant_id: str = DEFAULT_ASSISTANT_ID
    project_root: str | None = None
    system_prompt: str | None = None
    interactive: bool = True
    auto_approve: bool = False
    no_mcp: bool = False
    enable_shell: bool = True
    enable_interpreter: bool = True
    cwd: str | None = None
    host: str = "0.0.0.0"
    port: int = 10102

    def to_env(self) -> dict[str, str]:
        """Serialize configuration into environment variables."""
        env: dict[str, str] = {}
        if self.model is not None:
            env[f"{SERVER_ENV_PREFIX}MODEL"] = self.model
        if self.model_params is not None:
            env[f"{SERVER_ENV_PREFIX}MODEL_PARAMS"] = json.dumps(self.model_params)
        if self.profile_override is not None:
            env[f"{SERVER_ENV_PREFIX}PROFILE_OVERRIDE"] = json.dumps(self.profile_override)
        env[f"{SERVER_ENV_PREFIX}ASSISTANT_ID"] = self.assistant_id
        if self.project_root is not None:
            env[f"{SERVER_ENV_PREFIX}PROJECT_ROOT"] = self.project_root
        if self.system_prompt is not None:
            env[f"{SERVER_ENV_PREFIX}SYSTEM_PROMPT"] = self.system_prompt
        env[f"{SERVER_ENV_PREFIX}INTERACTIVE"] = str(self.interactive).lower()
        env[f"{SERVER_ENV_PREFIX}AUTO_APPROVE"] = str(self.auto_approve).lower()
        env[f"{SERVER_ENV_PREFIX}NO_MCP"] = str(self.no_mcp).lower()
        env[f"{SERVER_ENV_PREFIX}ENABLE_SHELL"] = str(self.enable_shell).lower()
        env[f"{SERVER_ENV_PREFIX}ENABLE_INTERPRETER"] = str(self.enable_interpreter).lower()
        if self.cwd is not None:
            env[f"{SERVER_ENV_PREFIX}CWD"] = self.cwd
        env[f"{SERVER_ENV_PREFIX}HOST"] = self.host
        env[f"{SERVER_ENV_PREFIX}PORT"] = str(self.port)
        return env

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Hydrate configuration from environment variables."""
        return cls(
            model=_read_env_str("MODEL"),
            model_params=_read_env_json("MODEL_PARAMS"),
            profile_override=_read_env_json("PROFILE_OVERRIDE"),
            assistant_id=_read_env_str("ASSISTANT_ID") or DEFAULT_ASSISTANT_ID,
            project_root=_read_env_str("PROJECT_ROOT"),
            system_prompt=_read_env_str("SYSTEM_PROMPT"),
            interactive=_read_env_bool("INTERACTIVE", default=True),
            auto_approve=_read_env_bool("AUTO_APPROVE", default=False),
            no_mcp=_read_env_bool("NO_MCP", default=False),
            enable_shell=_read_env_bool("ENABLE_SHELL", default=True),
            enable_interpreter=_read_env_bool("ENABLE_INTERPRETER", default=True),
            cwd=_read_env_str("CWD"),
            host=_read_env_str("HOST") or "0.0.0.0",
            port=_read_env_int("PORT", default=10102) or 10102,
        )
