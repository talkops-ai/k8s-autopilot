"""Unit tests for MCP Semantic Profiler."""

from __future__ import annotations

from unittest.mock import MagicMock

from k8s_autopilot.mcp.semantic_profiler import MCPSemanticProfiler, ToolSafetyProfile


def test_heuristic_profile_readonly():
    """Verify inspection MCP tools profile as Tier 1."""
    profiler = MCPSemanticProfiler.get_instance()

    tool = MagicMock()
    tool.name = "helm_list_releases"
    tool.description = "List all installed Helm releases"
    tool.annotations = {"readOnlyHint": True}

    profile = profiler.heuristic_profile(tool, server_name="helm-server")
    assert profile.inferred_tier == 1
    assert profile.read_only_hint is True
    assert profile.destructive_hint is False


def test_heuristic_profile_destructive():
    """Verify destructive MCP tools profile as Tier 4."""
    profiler = MCPSemanticProfiler.get_instance()

    tool = MagicMock()
    tool.name = "helm_uninstall_release"
    tool.description = "Uninstall a Helm release from cluster"
    tool.annotations = {"destructiveHint": True}

    profile = profiler.heuristic_profile(tool, server_name="helm-server")
    assert profile.inferred_tier == 4
    assert profile.read_only_hint is False
    assert profile.destructive_hint is True


def test_cache_and_lookup():
    """Verify registration and lookup by scoped and unscoped name."""
    profiler = MCPSemanticProfiler.get_instance()

    custom_profile = ToolSafetyProfile(
        tool_name="zap_cache",
        inferred_tier=4,
        read_only_hint=False,
        destructive_hint=True,
        sensitive_arguments=["force"],
        justification="Custom cache purge",
    )
    profiler.register_profile("custom-mcp", "zap_cache", custom_profile)

    scoped_hit = profiler.get_profile("custom-mcp", "zap_cache")
    assert scoped_hit is not None
    assert scoped_hit.inferred_tier == 4

    unscoped_hit = profiler.get_profile("", "custom-mcp:zap_cache")
    assert unscoped_hit is not None
    assert unscoped_hit.inferred_tier == 4


def test_heuristic_profile_generic_mcp_servers():
    """Verify generic prefix handling and token classification across different MCP domains."""
    profiler = MCPSemanticProfiler.get_instance()

    test_cases_readonly = [
        ("talkops-helm-mcp-server:kubernetes_get_helm_releases", "talkops-helm-mcp-server"),
        ("kubernetes_list_namespaces", "kubernetes-mcp"),
        ("argocd_get_application", "argocd-mcp"),
        ("prometheus_query_range", "observability-mcp"),
        ("loki_query_logs", "loki-mcp"),
        ("aws_describe_instances", "aws-mcp"),
        ("github_list_pull_requests", "github-mcp"),
        ("fetch_node_metrics", "metrics-server"),
        ("cluster_inspect_health", "diagnostics"),
    ]

    for tool_name, srv in test_cases_readonly:
        tool = MagicMock()
        tool.name = tool_name
        tool.description = "Inspection operation"
        tool.annotations = None
        profile = profiler.heuristic_profile(tool, server_name=srv)
        assert profile.inferred_tier == 1, f"Failed for {tool_name}"
        assert profile.read_only_hint is True, f"Failed for {tool_name}"
        assert profile.destructive_hint is False, f"Failed for {tool_name}"

    test_cases_destructive = [
        ("kubernetes_delete_pod", "kubernetes-mcp"),
        ("helm_uninstall_release", "helm-mcp"),
        ("argocd_delete_application", "argocd-mcp"),
        ("cluster_drain_node", "cluster-mcp"),
        ("purge_old_backups", "backup-mcp"),
        ("evict_pod", "k8s-mcp"),
    ]

    for tool_name, srv in test_cases_destructive:
        tool = MagicMock()
        tool.name = tool_name
        tool.description = "Destructive operation"
        tool.annotations = None
        profile = profiler.heuristic_profile(tool, server_name=srv)
        assert profile.inferred_tier == 4, f"Failed for {tool_name}"
        assert profile.read_only_hint is False, f"Failed for {tool_name}"
        assert profile.destructive_hint is True, f"Failed for {tool_name}"

