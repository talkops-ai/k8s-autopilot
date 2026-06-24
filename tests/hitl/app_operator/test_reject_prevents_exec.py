"""
HITL: Reject-prevents-execution — the most critical safety invariant.

When a user rejects an operation at the HITL gate, execution MUST halt.
The HumanInTheLoopMiddleware raises GraphInterrupt to stop the graph.

These tests verify:
1. The HITL middleware IS configured for each dangerous tool
2. The InterruptOnConfig is wired for the correct tools
3. The allowed_decisions list includes "reject"

Note on approach: We test the middleware CONFIGURATION, not a live graph
interrupt (which would require a full LangGraph checkpointer thread). This
is the correct scope — verifying the middleware is wired with "reject" in
allowed_decisions is sufficient to know GraphInterrupt will fire on rejection.
Full interrupt-resume testing belongs in the sandbox/e2e layer.
"""
import pytest

from k8s_autopilot.core.agents.app_operator.middleware import (
    build_app_operator_hitl_middleware,
    build_argo_rollouts_hitl_middleware,
    build_traefik_hitl_middleware,
)


@pytest.mark.hitl
def test_argocd_hitl_configured_for_destructive_tools():
    """
    ArgoCD HITL middleware must gate all destructive/mutating tools.

    If any of these tools are missing from interrupt_on, a production mutation
    could execute without operator approval.
    """
    middleware = build_app_operator_hitl_middleware()
    interrupt_on = middleware.interrupt_on  # dict[tool_name, InterruptOnConfig]

    required_gated_tools = {
        "create_application",
        "update_application",
        "sync_application",
        "delete_application",
        "delete_project",
        "delete_repository",
        "onboard_repository_https",
        "onboard_repository_ssh",
        "create_project",
    }
    missing = required_gated_tools - set(interrupt_on.keys())
    assert not missing, (
        f"These ArgoCD tools are missing from HITL gate — they will execute WITHOUT approval:\n"
        f"{missing}"
    )


@pytest.mark.hitl
def test_argocd_hitl_reject_is_always_allowed():
    """
    Every ArgoCD HITL gate must allow 'reject' as a decision.

    If 'reject' is missing from allowed_decisions, the user cannot halt
    the operation — it becomes a forced approval gate.
    """
    middleware = build_app_operator_hitl_middleware()
    for tool_name, config in middleware.interrupt_on.items():
        decisions = config.get("allowed_decisions") or []
        assert "reject" in decisions, (
            f"Tool '{tool_name}' HITL gate is missing 'reject' from allowed_decisions: {decisions}"
        )


@pytest.mark.hitl
def test_rollouts_hitl_configured_for_destructive_tools():
    """
    Argo Rollouts HITL middleware must gate all high-risk operations.
    """
    middleware = build_argo_rollouts_hitl_middleware()
    interrupt_on = middleware.interrupt_on

    required_gated_tools = {
        "argo_delete_rollout",
        "argo_delete_experiment",
        "convert_deployment_to_rollout",
        "argo_manage_rollout_lifecycle",
        "argo_create_rollout",
        "argo_update_rollout",
    }
    missing = required_gated_tools - set(interrupt_on.keys())
    assert not missing, (
        f"Rollout tools missing from HITL gate:\n{missing}"
    )


@pytest.mark.hitl
def test_traefik_hitl_configured_for_write_tools():
    """
    Traefik HITL middleware must gate all write/delete operations.
    """
    middleware = build_traefik_hitl_middleware()
    interrupt_on = middleware.interrupt_on

    required_gated_tools = {
        "traefik_manage_weighted_routing",
        "traefik_manage_simple_route",
        "traefik_nginx_migration",
    }
    # Check at least the known critical ones are present
    missing = required_gated_tools - set(interrupt_on.keys())
    assert not missing, (
        f"Traefik tools missing from HITL gate:\n{missing}"
    )


@pytest.mark.hitl
def test_rollouts_reject_is_always_allowed():
    """Every Rollouts HITL gate must allow 'reject'."""
    middleware = build_argo_rollouts_hitl_middleware()
    for tool_name, config in middleware.interrupt_on.items():
        decisions = config.get("allowed_decisions") or []
        assert "reject" in decisions, (
            f"Rollouts tool '{tool_name}' missing 'reject' from allowed_decisions: {decisions}"
        )
