"""Custom exceptions for K8s Autopilot."""

from __future__ import annotations


class K8sAutopilotError(Exception):
    """Base exception for all k8s-autopilot errors."""


class ModelConfigError(K8sAutopilotError):
    """Model configuration is invalid or incomplete."""


class MissingCredentialsError(ModelConfigError):
    """Required API credentials are not configured."""

    def __init__(self, message: str, *, provider: str = "", env_var: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.env_var = env_var


class MissingProviderPackageError(ModelConfigError):
    """Required LangChain provider package is not installed."""

    def __init__(self, message: str, *, provider: str = "", package: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.package = package


class NoCredentialsConfiguredError(MissingCredentialsError):
    """No credentials configured for any auto-detectable provider."""

    def __init__(self, message: str) -> None:
        super().__init__(message, provider="", env_var=None)
