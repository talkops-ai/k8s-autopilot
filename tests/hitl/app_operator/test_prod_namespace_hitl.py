"""
HITL: Production namespace warnings — most critical safety check.

These tests verify that the HITL approval card escalates to a 🚨 PRODUCTION
warning when the target namespace is a known production namespace.

Without these tests, a refactor of _is_production_namespace() or the card
builders could silently remove the production warnings, allowing operators
to approve prod operations without seeing the danger signal.
"""
import pytest

from k8s_autopilot.core.agents.app_operator.middleware import (
    _build_approval_description,
    _build_rollouts_approval_description,
    _build_traefik_approval_description,
)

# These namespaces must always trigger the PRODUCTION warning
PROD_NAMESPACES = ["production", "prod", "live", "prd"]
# These namespaces must NEVER trigger the PRODUCTION warning
NON_PROD_NAMESPACES = ["staging", "dev", "qa", "preview", "canary-demo"]


@pytest.mark.hitl
@pytest.mark.parametrize("namespace", PROD_NAMESPACES)
def test_create_app_prod_warning(namespace):
    """
    Creating an application in a production namespace must show 🚨 PRODUCTION warning.

    This is the most critical HITL assertion: operators must see the danger
    signal before approving production changes.
    """
    card = _build_approval_description(
        "create_application",
        {"name": "frontend", "destination_namespace": namespace}
    )
    assert "PRODUCTION" in card, (
        f"Card for namespace '{namespace}' must contain 'PRODUCTION' warning.\n"
        f"Actual card:\n{card}"
    )
    assert "🚨" in card, (
        f"Production card must use 🚨 emoji for namespace '{namespace}'.\n"
        f"Actual card:\n{card}"
    )


@pytest.mark.hitl
@pytest.mark.parametrize("namespace", NON_PROD_NAMESPACES)
def test_create_app_staging_no_warning(namespace):
    """
    Non-production namespaces must NOT show the 🚨 PRODUCTION escalation.

    A false positive here would cause alert fatigue — operators would start
    ignoring the warning if it fires for staging too.
    """
    card = _build_approval_description(
        "create_application",
        {"name": "frontend", "destination_namespace": namespace}
    )
    # Standard warning is fine, but not the escalated PRODUCTION header
    assert "🚨 **PRODUCTION" not in card, (
        f"Non-prod namespace '{namespace}' must NOT show 🚨 PRODUCTION header.\n"
        f"Actual card:\n{card}"
    )


@pytest.mark.hitl
@pytest.mark.parametrize("namespace", PROD_NAMESPACES)
def test_sync_prod_warning(namespace):
    """Syncing to a production namespace must show PRODUCTION in card."""
    card = _build_approval_description(
        "sync_application",
        {"name": "frontend", "namespace": namespace}
    )
    assert "PRODUCTION" in card


@pytest.mark.hitl
@pytest.mark.parametrize("namespace", PROD_NAMESPACES)
def test_delete_rollout_prod(namespace):
    """Deleting a rollout in production must show PRODUCTION escalation."""
    card = _build_rollouts_approval_description(
        "argo_delete_rollout",
        {"name": "frontend", "namespace": namespace}
    )
    assert "PRODUCTION" in card
    assert "🚨" in card


@pytest.mark.hitl
@pytest.mark.parametrize("namespace", PROD_NAMESPACES)
def test_weighted_routing_prod(namespace):
    """Creating a weighted route in production must show PRODUCTION in card."""
    card = _build_traefik_approval_description(
        "traefik_manage_weighted_routing",
        {
            "namespace": namespace,
            "action": "create",
            "route_name": "frontend-route",
            "stable_service": "frontend-stable",
            "canary_service": "frontend-canary",
            "stable_weight": 80,
            "canary_weight": 20,
        }
    )
    assert "PRODUCTION" in card
