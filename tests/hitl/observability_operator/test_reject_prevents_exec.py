"""
HITL: Reject-prevents-execution — the most critical safety invariant.

When a user rejects an operation at the HITL gate, execution MUST halt.
The HumanInTheLoopMiddleware raises GraphInterrupt to stop the graph.

These tests verify:
1. The HITL middleware IS configured for each domain's dangerous tools
2. The allowed_decisions list includes "reject"
3. No domain has accidentally dropped its reject capability

Note on approach: We test the middleware CONFIGURATION, not a live graph
interrupt (which would require a full LangGraph checkpointer thread). This
is the correct scope — verifying the middleware is wired with "reject" in
allowed_decisions is sufficient to know GraphInterrupt will fire on rejection.
Full interrupt-resume testing belongs in the sandbox/e2e layer.
"""
import pytest

from k8s_autopilot.core.agents.observability.middleware import (
    build_prometheus_hitl_middleware,
    build_alertmanager_hitl_middleware,
    build_opentelemetry_hitl_middleware,
    build_tempo_hitl_middleware,
)


@pytest.mark.hitl
def test_prometheus_reject_is_always_allowed():
    """Every Prometheus HITL gate must allow 'reject'."""
    middleware = build_prometheus_hitl_middleware()
    for tool_name, config in middleware.interrupt_on.items():
        decisions = config.get("allowed_decisions") or []
        assert "reject" in decisions, (
            f"Prometheus tool '{tool_name}' missing 'reject' from allowed_decisions: {decisions}"
        )


@pytest.mark.hitl
def test_alertmanager_reject_is_always_allowed():
    """Every Alertmanager HITL gate must allow 'reject'."""
    middleware = build_alertmanager_hitl_middleware()
    for tool_name, config in middleware.interrupt_on.items():
        decisions = config.get("allowed_decisions") or []
        assert "reject" in decisions, (
            f"Alertmanager tool '{tool_name}' missing 'reject' from allowed_decisions: {decisions}"
        )


@pytest.mark.hitl
def test_opentelemetry_reject_is_always_allowed():
    """Every OTel HITL gate must allow 'reject'."""
    middleware = build_opentelemetry_hitl_middleware()
    for tool_name, config in middleware.interrupt_on.items():
        decisions = config.get("allowed_decisions") or []
        assert "reject" in decisions, (
            f"OTel tool '{tool_name}' missing 'reject' from allowed_decisions: {decisions}"
        )


@pytest.mark.hitl
def test_tempo_reject_is_always_allowed():
    """Every Tempo HITL gate must allow 'reject'."""
    middleware = build_tempo_hitl_middleware()
    for tool_name, config in middleware.interrupt_on.items():
        decisions = config.get("allowed_decisions") or []
        assert "reject" in decisions, (
            f"Tempo tool '{tool_name}' missing 'reject' from allowed_decisions: {decisions}"
        )
