"""Configuration model for sandboxed backend execution environments."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass
class SandboxConfig:
    """Settings required to provision, configure, and connect to a sandbox environment."""

    provider: str = "local"
    """Target provider identifier ('local', 'langsmith', 'daytona', 'modal', 'runloop')."""

    sandbox_id: str | None = None
    """Optional existing remote sandbox ID to reconnect to."""

    image: str | None = None
    """Optional container base image name or Dockerfile ref."""

    setup_script_path: str | None = None
    """Optional path to a shell script to run inside the sandbox on boot."""

    snapshot_name: str | None = None
    """Optional snapshot or blueprint name to boot from (supported by LangSmith/Runloop)."""

    sync_workspace: bool = True
    """Whether to automatically synchronize local project files into the sandbox (default True)."""

    @classmethod
    def from_env(cls) -> SandboxConfig:
        """Load sandbox configuration from environment variables."""
        provider = os.getenv("K8S_SANDBOX_PROVIDER") or os.getenv("SANDBOX_PROVIDER") or "local"
        sandbox_id = os.getenv("K8S_SANDBOX_ID") or os.getenv("SANDBOX_ID")
        image = os.getenv("K8S_SANDBOX_IMAGE") or os.getenv("SANDBOX_IMAGE")
        setup_script_path = os.getenv("K8S_SANDBOX_SETUP_SCRIPT") or os.getenv("SANDBOX_SETUP_SCRIPT")
        snapshot_name = os.getenv("K8S_SANDBOX_SNAPSHOT") or os.getenv("SANDBOX_SNAPSHOT")
        sync_raw = os.getenv("K8S_SANDBOX_SYNC_WORKSPACE") or os.getenv("SANDBOX_SYNC_WORKSPACE")
        sync_workspace = sync_raw.lower() not in ("false", "0", "no") if sync_raw is not None else True

        provider = provider.lower().strip()
        if provider == "none" or not provider:
            provider = "local"

        return cls(
            provider=provider,
            sandbox_id=sandbox_id,
            image=image,
            setup_script_path=setup_script_path,
            snapshot_name=snapshot_name,
            sync_workspace=sync_workspace,
        )
