"""
HITL: Traefik approval card content verification.

Tests the Traefik description builder produces actionable cards for each
operation type. Operators must see route names, namespaces, weights, and
traffic impact before approving.
"""
import pytest

from k8s_autopilot.core.agents.app_operator.middleware import _build_traefik_approval_description


@pytest.mark.hitl
@pytest.mark.parametrize("tool_name,tool_args,expected_in_card", [
    # Weighted routing: route name, weights shown
    ("traefik_manage_weighted_routing",
     {"namespace": "staging", "action": "create", "route_name": "frontend-route",
      "stable_service": "frontend-stable", "canary_service": "frontend-canary",
      "stable_weight": 80, "canary_weight": 20},
     "80%"),

    ("traefik_manage_weighted_routing",
     {"namespace": "staging", "action": "create", "route_name": "frontend-route",
      "stable_service": "frontend-stable", "canary_service": "frontend-canary",
      "stable_weight": 80, "canary_weight": 20},
     "20%"),

    # Delete weighted route shows traffic-stop warning
    ("traefik_manage_weighted_routing",
     {"namespace": "staging", "action": "delete", "route_name": "old-route"},
     "traffic"),

    # Simple route shows service name
    ("traefik_manage_simple_route",
     {"namespace": "staging", "action": "create", "route_name": "api-route",
      "service_name": "api-svc"},
     "api-svc"),

    # NGINX migration generate: read-only label
    ("traefik_nginx_migration",
     {"namespace": "staging", "action": "generate"},
     "PREVIEW"),

    # NGINX migration apply: cluster mutation warning
    ("traefik_nginx_migration",
     {"namespace": "staging", "action": "apply"},
     "NGINX MIGRATION APPLY"),

    # TCP routing: no-rollback warning
    ("traefik_manage_tcp_routing",
     {"namespace": "staging", "action": "create", "route_name": "db-tcp",
      "service_name": "postgres", "service_port": 5432},
     "rollback"),

    # Middleware shows middleware type
    ("traefik_manage_middleware",
     {"namespace": "staging", "action": "create",
      "middleware_name": "rate-limiter", "middleware_type": "RateLimit"},
     "RateLimit"),
])
def test_traefik_hitl_card_content(tool_name, tool_args, expected_in_card):
    """Traefik approval card must contain expected text."""
    card = _build_traefik_approval_description(tool_name, tool_args)
    assert expected_in_card.lower() in card.lower(), (
        f"Card for '{tool_name}' (action={tool_args.get('action')}) "
        f"missing '{expected_in_card}'.\nActual card:\n{card}"
    )


@pytest.mark.hitl
def test_nginx_migration_generate_vs_apply():
    """
    NGINX migration generate (read-only) and apply (cluster mutation) cards
    must be clearly different so operators understand the risk level.
    """
    preview_card = _build_traefik_approval_description(
        "traefik_nginx_migration",
        {"namespace": "staging", "action": "generate"}
    )
    apply_card = _build_traefik_approval_description(
        "traefik_nginx_migration",
        {"namespace": "staging", "action": "apply"}
    )
    assert "no cluster changes" in preview_card.lower(), (
        f"Preview card must state 'No cluster changes'.\nCard:\n{preview_card}"
    )
    assert "no cluster changes" not in apply_card.lower(), (
        f"Apply card must NOT say 'No cluster changes'.\nCard:\n{apply_card}"
    )
