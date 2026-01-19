"""
ArgoCD Application Onboarding Agent Package

Provides the ArgoCDOnboardingAgent for managing ArgoCD projects,
repositories, and applications through a Deep Agent architecture.
"""

from k8s_autopilot.core.agents.onboarding.orchestrator_agent import (
    ArgoCDOnboardingAgent,
    create_argocd_onboarding_agent,
    create_argocd_onboarding_agent_factory,
)

__all__ = [
    "ArgoCDOnboardingAgent",
    "create_argocd_onboarding_agent",
    "create_argocd_onboarding_agent_factory",
]

