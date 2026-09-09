"""Unit tests for built-in subagents discovery, validation, and skill associations."""

from __future__ import annotations

import json
from pathlib import Path

from k8s_autopilot.subagents.loader import list_subagents
from k8s_autopilot.subagents.subagents_parser import parse_built_in_subagents


def test_discover_all_four_builtin_subagents() -> None:
    """Verify parse_built_in_subagents discovers exactly 4 built-in subagents with correct names."""
    built_in = parse_built_in_subagents()
    names = {agent["name"] for agent in built_in}

    assert names == {
        "helm-operator",
        "k8s-operator",
        "app-operator",
        "observability-operator",
    }


def test_builtin_subagent_details() -> None:
    """Verify tool permissions, skills, prompts, and MCP configurations of each subagent."""
    built_in = parse_built_in_subagents()
    by_name = {agent["name"]: agent for agent in built_in}

    # 1. Helm Operator
    helm = by_name["helm-operator"]
    assert "Helm" in helm["description"]
    assert "helm-operation" in helm["skills"]
    assert "execute" in helm["tools"]
    assert "You are the Helm Operator" in helm["system_prompt"]
    assert "mcp_files" in helm

    # 2. Kubernetes Operator
    k8s = by_name["k8s-operator"]
    assert "Kubernetes" in k8s["description"]
    assert "kubernetes-cluster-ops" in k8s["skills"]
    assert "execute" in k8s["tools"]
    assert "You are the K8s Operator" in k8s["system_prompt"]
    assert "mcp_files" in k8s

    # 3. App Operator
    app = by_name["app-operator"]
    assert "ArgoCD" in app["description"]
    assert "argocd-gitops" in app["skills"]
    assert "argo-rollout-gitops" in app["skills"]
    assert "traefik-edge-routing" in app["skills"]
    assert "You are the App Operator" in app["system_prompt"]
    assert "mcp_files" in app

    # 4. Observability Operator
    obs = by_name["observability-operator"]
    assert "Prometheus" in obs["description"]
    assert "prometheus" in obs["skills"]
    assert "alertmanager" in obs["skills"]
    assert "loki" in obs["skills"]
    assert "opentelemetry" in obs["skills"]
    assert "tempo" in obs["skills"]
    assert "You are the Observability Operator" in obs["system_prompt"]
    assert "mcp_files" in obs


def test_builtin_mcp_config_validity() -> None:
    """Verify all .mcp.json files in built_in_subagents are valid JSON."""
    built_in_dir = Path(__file__).parent.parent.parent.parent / "k8s_autopilot" / "built_in_subagents"
    mcp_files = list(built_in_dir.rglob("*.mcp.json"))
    assert len(mcp_files) >= 4

    for mcp_file in mcp_files:
        content = mcp_file.read_text(encoding="utf-8")
        data = json.loads(content)
        assert "mcpServers" in data
        assert isinstance(data["mcpServers"], dict)
