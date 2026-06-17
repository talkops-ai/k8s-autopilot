"""
K8s Autopilot — Provider & Harness Profile registrations.

Registers provider-level defaults for every supported LLM provider.
Provider-level (not model-level) because this is an open-source project
where users are free to choose any model from a given provider.

This module is imported at coordinator startup for side-effect registration.

API References:
    - ProviderProfile:  https://docs.langchain.com/oss/python/deepagents/profiles#provider-profiles
    - HarnessProfile:   https://docs.langchain.com/oss/python/deepagents/profiles#harness-profiles
    - register_*:       ``from deepagents import register_provider_profile, register_harness_profile``
"""

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    ProviderProfile,
    register_harness_profile,
    register_provider_profile,
)

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("K8sAutopilotProfiles")


# ---------------------------------------------------------------------------
# Provider profiles — model construction defaults
# ---------------------------------------------------------------------------
# temperature=0 across all providers for deterministic operations
# (rule authoring, silence creation, CRD patching must be reproducible).

_PROVIDER_DEFAULTS = {
    "google_genai": ProviderProfile(init_kwargs={"temperature": 0}),
    "openai": ProviderProfile(init_kwargs={"temperature": 0}),
    "anthropic": ProviderProfile(init_kwargs={"temperature": 0}),
    "openrouter": ProviderProfile(init_kwargs={"temperature": 0}),
    "fireworks": ProviderProfile(init_kwargs={"temperature": 0}),
    "ollama": ProviderProfile(init_kwargs={"temperature": 0}),
    "aws": ProviderProfile(init_kwargs={"temperature": 0}),
}

for provider_key, profile in _PROVIDER_DEFAULTS.items():
    register_provider_profile(provider_key, profile)

logger.debug(
    f"Registered ProviderProfiles for {len(_PROVIDER_DEFAULTS)} providers "
    f"(temperature=0)"
)


# ---------------------------------------------------------------------------
# Harness profiles — agent behaviour after model construction
# ---------------------------------------------------------------------------
# We register provider-level harness profiles so they apply to ANY model
# from that provider.
#
# Key decisions:
#   1. Disable GP subagent — we have domain-specific subagents; the generic
#      GP subagent adds noise and uses context for nothing.
#   2. System prompt suffix — reminds the model of its operational domain.
#      Per-coordinator suffixes are applied via register_domain_profiles().

# Per-coordinator domain suffixes — each coordinator gets a suffix that
# accurately describes its scope.  This prevents cross-domain confusion
# when the agent's context is summarized (which strips the system prompt
# but preserves suffix-injected context from HarnessProfile).
DOMAIN_SUFFIXES = {
    "observability": (
        "\n\nYou are a production Kubernetes observability automation agent. "
        "All operations MUST be scoped to the user's configured cluster. "
        "NEVER hallucinate metric names, label values, or alert rules — "
        "always verify against live data via your MCP tools first."
    ),
    "helm": (
        "\n\nYou are a production Kubernetes Helm chart and release management agent. "
        "All operations MUST be scoped to the user's configured cluster. "
        "NEVER hallucinate chart names, release names, or repository URLs — "
        "always verify against live data via your MCP tools first."
    ),
    "app": (
        "\n\nYou are a production Kubernetes application lifecycle agent "
        "managing ArgoCD, Argo Rollouts, and Traefik edge routing. "
        "All operations MUST be scoped to the user's configured cluster. "
        "NEVER hallucinate application names, rollout parameters, or routing rules — "
        "always verify against live data via your MCP tools first."
    ),
    "k8s": (
        "\n\nYou are a production Kubernetes cluster operations agent. "
        "All operations MUST be scoped to the user's configured cluster and context. "
        "NEVER hallucinate resource names, namespaces, or cluster contexts — "
        "always verify against live data via your MCP tools first."
    ),
}

# Default suffix used when no domain is specified (backward-compatible).
_DEFAULT_OPS_SUFFIX = DOMAIN_SUFFIXES["observability"]


def _build_harness_profiles(suffix: str) -> dict:
    """Build provider→HarnessProfile mapping with the given suffix."""
    return {
        provider: HarnessProfile(
            system_prompt_suffix=suffix,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        )
        for provider in _PROVIDER_DEFAULTS
    }


def register_domain_profiles(domain: str) -> None:
    """Register harness profiles for a specific coordinator domain.

    This overwrites the provider-level harness profile with a domain-specific
    suffix.  Should be called at coordinator startup (side-effect import).

    Args:
        domain: One of 'observability', 'helm', 'app', 'k8s'.

    Ref: https://docs.langchain.com/oss/python/deepagents/profiles#harness-profiles
    """
    suffix = DOMAIN_SUFFIXES.get(domain, _DEFAULT_OPS_SUFFIX)
    profiles = _build_harness_profiles(suffix)
    for provider_key, profile in profiles.items():
        register_harness_profile(provider_key, profile)
    logger.debug(
        f"Registered HarnessProfiles for domain='{domain}' "
        f"({len(profiles)} providers, GP subagent disabled)"
    )


# ---------------------------------------------------------------------------
# Default registration (backward-compatible) — uses the observability suffix
# since it was the original default.  Each coordinator's __init__ or startup
# should call register_domain_profiles() with their own domain to override.
# ---------------------------------------------------------------------------

_HARNESS_DEFAULTS = _build_harness_profiles(_DEFAULT_OPS_SUFFIX)

for provider_key, profile in _HARNESS_DEFAULTS.items():
    register_harness_profile(provider_key, profile)

logger.debug(
    f"Registered HarnessProfiles for {len(_HARNESS_DEFAULTS)} providers "
    f"(GP subagent disabled, default ops suffix)"
)

