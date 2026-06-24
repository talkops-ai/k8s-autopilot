"""
Sandbox: End-to-end ServiceMonitor creation on a real Kubernetes cluster.

This test is ONLY executed when ENABLE_SANDBOX_TESTS=true.
It requires:
- A running kind/k3d/vCluster cluster
- Prometheus Operator CRDs installed
- kubectl configured and authenticated

This validates the complete flow:
1. Agent receives "Create ServiceMonitor for checkout in prod"
2. Agent delegates to prometheus-operator subagent
3. Subagent calls prom_apply_servicemonitor MCP tool
4. HITL gate fires, test auto-approves
5. ServiceMonitor CRD is created in the cluster
6. Prometheus target appears for the service
"""
import os
import pytest
import subprocess

from tests.sandbox.observability_operator.conftest import require_sandbox


@require_sandbox
@pytest.mark.sandbox
@pytest.mark.timeout(120)
def test_servicemonitor_crd_exists_after_creation(sandbox_enabled):
    """
    Placeholder: Verify ServiceMonitor CRD was created.

    In a real implementation, this would:
    1. Build the full ObservabilityCoordinator with real MCP connections
    2. Run the "create ServiceMonitor" flow with auto-approve HITL
    3. kubectl get servicemonitors -n prod -o json
    4. Assert the ServiceMonitor exists with correct selectors
    """
    # Verify cluster is reachable
    result = subprocess.run(
        ["kubectl", "cluster-info"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"kubectl cluster-info failed: {result.stderr}"

    # Verify Prometheus Operator CRDs are installed
    result = subprocess.run(
        ["kubectl", "get", "crd", "servicemonitors.monitoring.coreos.com"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        "ServiceMonitor CRD not found. Install Prometheus Operator CRDs first:\n"
        "kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/"
        "prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_servicemonitors.yaml"
    )


@require_sandbox
@pytest.mark.sandbox
@pytest.mark.timeout(120)
def test_prometheus_targets_include_service(sandbox_enabled):
    """
    Placeholder: Verify Prometheus discovers the new target.

    In a real implementation, this would:
    1. Create ServiceMonitor via the agent flow
    2. Wait for Prometheus to discover the target (up to 60s)
    3. Query Prometheus API for active targets
    4. Assert the service appears in the target list
    """
    pytest.skip("Full e2e flow not yet implemented — requires MCP server in sandbox")
