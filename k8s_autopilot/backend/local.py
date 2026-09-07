"""Local shell and filesystem backend for K8s Autopilot.

Extends ``LocalShellBackend`` with a curated shell
environment that preserves Kubernetes, Helm, ArgoCD, and observability
tool environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend as SDKLocalShellBackend

from k8s_autopilot.backend.registry import register_backend

# K8s-specific environment variables to preserve in the shell sandbox.
K8S_PRESERVE_ENV_VARS: frozenset[str] = frozenset({
    # Kubernetes
    "KUBECONFIG", "KUBE_NAMESPACE", "KUBE_CONTEXT", "KUBE_CLUSTER",
    # Helm
    "HELM_HOME", "HELM_CACHE_HOME", "HELM_CONFIG_HOME",
    "HELM_DATA_HOME", "HELM_DRIVER", "HELM_REGISTRY_CONFIG",
    # ArgoCD
    "ARGOCD_AUTH_TOKEN", "ARGOCD_SERVER", "ARGOCD_OPTS",
    "ARGOCD_GRPC_WEB", "ARGOCD_SERVER_NAME",
    # Observability
    "PROMETHEUS_URL", "ALERTMANAGER_URL", "LOKI_URL", "TEMPO_URL",
    "GRAFANA_URL", "GRAFANA_TOKEN",
    # Traefik
    "TRAEFIK_API_URL",
    # Cloud providers
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CORE_PROJECT",
    "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID",
})


def _build_shell_env() -> dict[str, str]:
    """Build a curated, secure shell environment with K8s-specific variables."""
    safe_keys = {
        "PATH", "HOME", "SHELL", "TERM", "LANG", "USER", "LOGNAME", "PWD",
        "EDITOR", "VISUAL", "LC_ALL", "LOCALE",
    }
    env: dict[str, str] = {}
    for key in safe_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    # Prepend common binary directories to PATH
    path_val = env.get("PATH", "")
    for tool_dir in ["/usr/local/bin", "~/.local/bin"]:
        expanded = os.path.expanduser(tool_dir)
        if expanded not in path_val:
            path_val = f"{expanded}:{path_val}"
    env["PATH"] = path_val

    # Copy K8s-specific environment variables
    for key, val in os.environ.items():
        if key in K8S_PRESERVE_ENV_VARS:
            env[key] = val
        elif key.startswith(("KUBE_", "HELM_", "ARGOCD_", "KUBECTL_")):
            env[key] = val

    return env


@register_backend("local")
class LocalShellBackend(SDKLocalShellBackend):
    """Local shell execution with K8s-specific environment."""

    def __init__(
        self,
        root_dir: Path | str,
        virtual_mode: bool = False,
        inherit_env: bool = False,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        effective_env = env if env is not None else _build_shell_env()
        super().__init__(
            root_dir=Path(root_dir),
            virtual_mode=virtual_mode,
            inherit_env=inherit_env,
            env=effective_env,
            **kwargs,
        )
