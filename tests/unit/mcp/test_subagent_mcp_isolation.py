"""Unit tests for Subagent MCP Server isolation (BUG-MCP-001).

Verifies:
1. MCP discovery does not scan or return built-in subagent .mcp.json files.
2. Legacy builtin:* servers in DB are purged during discover_and_sync_async.
3. Coordinator agent does not register subagent MCP tools in all_tools.
4. Built-in subagents retain their scoped mcp_files and receive MCPContextMiddleware.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
from k8s_autopilot.config.adapters.sqlite import SqliteConfigAdapter
from k8s_autopilot.config.store import ConfigStore
from k8s_autopilot.mcp.discovery import MCPDiscovery, discover_mcp_configs
from k8s_autopilot.middleware.mcp_context import MCPContextMiddleware
from k8s_autopilot.subagents.loader import get_built_in_subagents


BUILTIN_MCP_SERVER_NAMES = {
    "talkops-helm-mcp-server",
    "talkops-kubernetes-mcp-server",
    "talkops-argo-rollout-mcp-server",
    "talkops-argocd-mcp-server",
    "talkops-traefik-mcp-server",
    "talkops-prometheus-mcp-server",
    "talkops-alertmanager-mcp-server",
    "talkops-loki-mcp-server",
    "talkops-opentelemetry-mcp-server",
    "talkops-tempo-mcp-server",
}


def test_mcp_discovery_excludes_builtin_subagents(tmp_path: Path) -> None:
    """Verify discover_mcp_configs does not discover built-in subagent MCP servers."""
    configs = discover_mcp_configs(project_root=tmp_path)
    for builtin_server in BUILTIN_MCP_SERVER_NAMES:
        assert builtin_server not in configs, (
            f"Built-in subagent MCP server '{builtin_server}' should not be returned by global discovery"
        )


@pytest.mark.asyncio
async def test_mcp_discovery_purges_legacy_builtin_servers(tmp_path: Path) -> None:
    """Verify discover_and_sync_async purges legacy builtin:* servers from ConfigStore."""
    db_path = tmp_path / "test_config.db"
    adapter = SqliteConfigAdapter(db_path)
    store = ConfigStore(adapter)
    await store.initialize()

    # Pre-seed DB with a legacy builtin server and a user server
    await store.upsert_mcp_server({
        "name": "talkops-helm-mcp-server",
        "transport": "stdio",
        "command": "helm-mcp-server",
        "args": [],
        "env": {},
        "source": "builtin:helm-operator",
        "enabled": True,
        "trusted": True,
    })
    await store.upsert_mcp_server({
        "name": "custom-user-mcp-server",
        "transport": "http",
        "url": "http://localhost:8080",
        "source": "user",
        "enabled": True,
        "trusted": False,
    })

    # Verify both exist before sync
    db_before = {s["name"]: s for s in await store.list_mcp_servers()}
    assert "talkops-helm-mcp-server" in db_before
    assert "custom-user-mcp-server" in db_before

    # Run discovery and sync
    discovery = MCPDiscovery(store=store)
    synced = await discovery.discover_and_sync_async(project_root=tmp_path, store=store)

    # Verify legacy builtin server was purged
    assert "talkops-helm-mcp-server" not in synced
    assert "custom-user-mcp-server" in synced

    db_after = {s["name"]: s for s in await store.list_mcp_servers()}
    assert "talkops-helm-mcp-server" not in db_after
    assert "custom-user-mcp-server" in db_after


def test_builtin_subagents_retain_mcp_files() -> None:
    """Verify built-in subagents still have mcp_files configured pointing to their bundle .mcp.json."""
    subagents = get_built_in_subagents()
    by_name = {agent["name"]: agent for agent in subagents}

    for name in ["helm-operator", "k8s-operator", "app-operator", "observability-operator"]:
        assert name in by_name, f"Missing built-in subagent: {name}"
        meta = by_name[name]
        assert "mcp_files" in meta, f"Subagent '{name}' must have mcp_files configured"
        mcp_files = meta["mcp_files"]
        assert len(mcp_files) >= 1
        assert Path(mcp_files[0]).is_file()


@pytest.mark.asyncio
async def test_coordinator_agent_does_not_attach_subagent_mcp_tools(tmp_path: Path) -> None:
    """Verify create_k8s_autopilot_agent does not attach subagent MCP tools to coordinator."""
    with patch("k8s_autopilot.agent.factory.create_deep_agent") as mock_create_deep_agent:
        mock_create_deep_agent.return_value = MagicMock()

        agent, backend = create_k8s_autopilot_agent(
            model="google_genai:gemini-3.7-flash",
            cwd=tmp_path,
            auto_approve=True,
        )

        assert mock_create_deep_agent.called
        call_kwargs = mock_create_deep_agent.call_args.kwargs

        tools = call_kwargs.get("tools", [])
        tool_names = [getattr(t, "name", str(t)) for t in tools]

        # Coordinator should not have any built-in MCP server prefixed tools
        for tool_name in tool_names:
            for srv in BUILTIN_MCP_SERVER_NAMES:
                assert not tool_name.startswith(f"{srv}:"), (
                    f"Coordinator tool '{tool_name}' leaked from subagent server '{srv}'"
                )

        # Check subagents have MCPContextMiddleware
        subagents = call_kwargs.get("subagents", [])
        subagents_by_name = {s["name"]: s for s in subagents if isinstance(s, dict)}

        for expected_name in ["helm-operator", "k8s-operator", "app-operator", "observability-operator"]:
            assert expected_name in subagents_by_name
            sub = subagents_by_name[expected_name]
            mw_list = sub.get("middleware", [])
            has_mcp_context_mw = any(isinstance(mw, MCPContextMiddleware) for mw in mw_list)
            assert has_mcp_context_mw, (
                f"Subagent '{expected_name}' must have MCPContextMiddleware configured"
            )
            sub_tools = sub.get("tools", [])
            assert len(sub_tools) > 0, (
                f"Subagent '{expected_name}' must have its scoped MCP tools attached"
            )

        # Verify MCPSessionManager accumulated all subagents servers
        from k8s_autopilot.mcp.session_manager import MCPSessionManager
        mgr = MCPSessionManager.get_instance()
        assert "talkops-helm-mcp-server" in mgr._config, (
            "talkops-helm-mcp-server must be registered in MCPSessionManager"
        )
        assert "talkops-kubernetes-mcp-server" in mgr._config, (
            "talkops-kubernetes-mcp-server must be registered in MCPSessionManager"
        )


def test_subagent_mcp_configs_adapter_for_builtin_subagent() -> None:
    """Verify subagent_mcp_configs adapter properly resolves built-in subagent MCP configs."""
    from pathlib import Path
    from k8s_autopilot.plugins.adapters.mcp import subagent_mcp_configs

    builtin_root = (
        Path(__file__).parent.parent.parent.parent
        / "k8s_autopilot"
        / "built_in_subagents"
    )
    helm_dir = builtin_root / "helm-operator"
    configs = subagent_mcp_configs("helm-operator", helm_dir)
    assert "talkops-helm-mcp-server" in configs
    assert configs["talkops-helm-mcp-server"]["command"] == "helm-mcp-server"
    assert "subagent__helm-operator__talkops-helm-mcp-server" in configs

