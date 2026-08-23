import os
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class SandboxConfig:
    """Configuration options for the deep agent execution sandbox."""

    provider: str
    """Sandbox provider name (e.g. 'langsmith', 'modal', 'daytona', 'runloop', 'local').
    
    Defaults to 'local'.
    """

    sandbox_id: Optional[str] = None
    """Optional existing sandbox ID to reuse or reconnect to."""

    image: Optional[str] = None
    """Optional Docker image to boot the sandbox from."""

    setup_script_path: Optional[str] = None
    """Optional path to a shell script to run inside the sandbox on boot."""

    snapshot_name: Optional[str] = None
    """Optional snapshot or blueprint name to boot from (supported by LangSmith/Runloop)."""

    sync_workspace: bool = True
    """Whether to automatically synchronize local project files into the sandbox (default True)."""

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        """Load sandbox configuration from the central Config engine, falling back to environment variables."""
        try:
            from k8s_autopilot.config.config import Config
            cfg = Config()
            provider = cfg.K8S_SANDBOX_PROVIDER
            sandbox_id = cfg.K8S_SANDBOX_ID
            image = cfg.K8S_SANDBOX_IMAGE
            setup_script_path = cfg.K8S_SANDBOX_SETUP_SCRIPT
            snapshot_name = cfg.K8S_SANDBOX_SNAPSHOT
            sync_workspace = cfg.K8S_SANDBOX_SYNC_WORKSPACE
            
            # Ensure proper fallback if config attributes are empty
            if not provider:
                provider = "local"
            if sync_workspace is None:
                sync_workspace = True
        except Exception:
            # Fallback to direct environment lookups
            provider = os.getenv("K8S_SANDBOX_PROVIDER", "local").lower().strip()
            sandbox_id = os.getenv("K8S_SANDBOX_ID")
            image = os.getenv("K8S_SANDBOX_IMAGE")
            setup_script_path = os.getenv("K8S_SANDBOX_SETUP_SCRIPT")
            snapshot_name = os.getenv("K8S_SANDBOX_SNAPSHOT")
            sync_workspace_str = os.getenv("K8S_SANDBOX_SYNC_WORKSPACE", "true").lower().strip()
            sync_workspace = sync_workspace_str in ("true", "1", "yes")

        # Normalize provider
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
