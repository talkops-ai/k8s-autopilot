"""
Sandbox test fixtures for Observability Operator.

All sandbox tests are gated behind the ENABLE_SANDBOX_TESTS environment
variable. Set it to "true" or "1" to enable sandbox testing against an
ephemeral K8s cluster (kind/k3d/vCluster).

By default, this switch is FALSE — sandbox tests are skipped in CI
unless explicitly enabled for release validation.
"""
import os
import pytest


# ── Global switch ─────────────────────────────────────────────────────────
ENABLE_SANDBOX_TESTS = os.environ.get("ENABLE_SANDBOX_TESTS", "false").lower() in ("true", "1", "yes")


def require_sandbox(fn):
    """Decorator: skip test unless ENABLE_SANDBOX_TESTS=true."""
    return pytest.mark.skipif(
        not ENABLE_SANDBOX_TESTS,
        reason="Sandbox tests disabled. Set ENABLE_SANDBOX_TESTS=true to run.",
    )(fn)


@pytest.fixture(scope="module")
def sandbox_enabled():
    """Module-level guard — skip the entire module if sandbox is disabled."""
    if not ENABLE_SANDBOX_TESTS:
        pytest.skip("Sandbox tests disabled. Set ENABLE_SANDBOX_TESTS=true to run.")
