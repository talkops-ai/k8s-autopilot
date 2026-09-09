"""Unit tests for MCP environment variable propagation, connection creation DRY helper,
bidirectional alias synchronization, and cluster DNS error diagnostics.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_mcp_adapters.sessions import (
    SSEConnection,
    StdioConnection,
    StreamableHttpConnection,
)

from k8s_autopilot.config.settings import sync_mcp_env_aliases
from k8s_autopilot.mcp.session_manager import (
    _enhance_mcp_error_diagnostics,
    create_mcp_connection,
)


def test_create_mcp_connection_stdio_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test StdioConnection inherits ambient os.environ and merges server-specific env."""
    monkeypatch.setenv("TEST_AMBIENT_VAR", "ambient_val")
    monkeypatch.setenv("OVERRIDE_VAR", "ambient_override")

    config = {
        "type": "stdio",
        "command": "test-server",
        "args": ["--debug"],
        "env": {
            "OVERRIDE_VAR": "server_override",
            "CUSTOM_SERVER_VAR": "server_val",
        },
    }

    conn = create_mcp_connection(config)
    assert conn["transport"] == "stdio"
    assert conn["command"] == "test-server"
    assert conn["args"] == ["--debug"]
    assert conn["env"]["TEST_AMBIENT_VAR"] == "ambient_val"
    assert conn["env"]["OVERRIDE_VAR"] == "server_override"
    assert conn["env"]["CUSTOM_SERVER_VAR"] == "server_val"


def test_create_mcp_connection_transports() -> None:
    """Test create_mcp_connection instantiates correct transport connections."""
    # HTTP
    http_cfg = {
        "type": "http",
        "url": "http://localhost:8080/mcp",
        "headers": {"Authorization": "Bearer token"},
    }
    http_conn = create_mcp_connection(http_cfg)
    assert http_conn["transport"] == "streamable_http"
    assert http_conn["url"] == "http://localhost:8080/mcp"
    assert http_conn["headers"] == {"Authorization": "Bearer token"}

    # SSE
    sse_cfg = {
        "type": "sse",
        "url": "http://localhost:8080/events",
        "headers": {"X-Custom": "val"},
    }
    sse_conn = create_mcp_connection(sse_cfg)
    assert sse_conn["transport"] == "sse"
    assert sse_conn["url"] == "http://localhost:8080/events"

    # URL inference
    inferred_sse = create_mcp_connection({"url": "http://localhost:8080/mcp/sse"})
    assert inferred_sse["transport"] == "sse"

    inferred_http = create_mcp_connection({"url": "http://localhost:8080/mcp"})
    assert inferred_http["transport"] == "streamable_http"


def test_sync_mcp_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test bidirectional synchronization of Prometheus and Kubeconfig aliases."""
    # Case 1: PROMETHEUS_BASE_URL set -> syncs to PROMETHEUS_URL
    monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prom:9090")
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("PROMETHEUS_URL") == "http://prom:9090"

    # Case 2: PROMETHEUS_URL set -> syncs to PROMETHEUS_BASE_URL
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom-alt:9090")
    monkeypatch.delenv("PROMETHEUS_BASE_URL", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("PROMETHEUS_BASE_URL") == "http://prom-alt:9090"

    # Case 3: KUBECONFIG set -> syncs to K8S_KUBECONFIG
    monkeypatch.setenv("KUBECONFIG", "/custom/kube/config")
    monkeypatch.delenv("K8S_KUBECONFIG", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("K8S_KUBECONFIG") == "/custom/kube/config"

    # Case 4: K8S_KUBECONFIG set -> syncs to KUBECONFIG
    monkeypatch.setenv("K8S_KUBECONFIG", "/alt/kube/config")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("KUBECONFIG") == "/alt/kube/config"


def test_enhance_mcp_error_diagnostics_cluster_dns() -> None:
    """Test that cluster DNS errors receive actionable port-forwarding instructions."""
    error = "Failed to connect to https://argocd-server.argocd.svc:443: [Errno -2] Name or service not known"
    enhanced = _enhance_mcp_error_diagnostics(error)
    assert "[Diagnostic Hint:" in enhanced
    assert "kubectl port-forward" in enhanced
    assert "https://argocd-server.argocd.svc:443" in enhanced

    # Unrelated error receives no diagnostic hint
    normal_error = "Invalid parameter 'namespace': must be non-empty string"
    unaltered = _enhance_mcp_error_diagnostics(normal_error)
    assert unaltered == normal_error
    assert "[Diagnostic Hint:" not in unaltered


def test_subagent_mcp_manifests_validity() -> None:
    """Verify built-in subagent .mcp.json files contain expected environment variable templates."""
    base_dir = Path("k8s_autopilot/built_in_subagents")

    # Observability operator
    obs_mcp = json.loads((base_dir / "observability-operator" / ".mcp.json").read_text(encoding="utf-8"))
    obs_servers = obs_mcp["mcpServers"]
    assert "talkops-prometheus-mcp-server" in obs_servers
    assert "PROMETHEUS_BASE_URL" in obs_servers["talkops-prometheus-mcp-server"]["env"]
    assert "talkops-loki-mcp-server" in obs_servers
    assert "LOKI_URL" in obs_servers["talkops-loki-mcp-server"]["env"]
    assert "talkops-tempo-mcp-server" in obs_servers
    assert "TEMPO_BASE_URL" in obs_servers["talkops-tempo-mcp-server"]["env"]
    assert "talkops-alertmanager-mcp-server" in obs_servers
    assert "ALERTMANAGER_BASE_URL" in obs_servers["talkops-alertmanager-mcp-server"]["env"]

    # App operator
    app_mcp = json.loads((base_dir / "app-operator" / ".mcp.json").read_text(encoding="utf-8"))
    app_servers = app_mcp["mcpServers"]
    assert "talkops-argocd-mcp-server" in app_servers
    assert "ARGOCD_SERVER_URL" in app_servers["talkops-argocd-mcp-server"]["env"]
    assert "talkops-argo-rollout-mcp-server" in app_servers
    assert "PROMETHEUS_URL" in app_servers["talkops-argo-rollout-mcp-server"]["env"]
