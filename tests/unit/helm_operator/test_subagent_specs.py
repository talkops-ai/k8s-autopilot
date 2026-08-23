"""
Tests for the filesystem-based subagent discovery (dcode-aligned).

Validates that:
- The subagent directories are discovered from memory/helm-operator/agents/
- Each subagent has required keys (name, description, system_prompt)
- MCP-connected subagents have correct frontmatter extensions
- The build_dynamic_subagent_spec() produces valid dict specs
"""

import pytest

from k8s_autopilot.core.agents.registry import (
    list_subagents,
    get_domain_agents_dir,
)
from k8s_autopilot.core.agents.helm_operator.middleware import (
    build_dynamic_subagent_spec,
)


@pytest.fixture
def helm_subagent_metas():
    """Discover subagents from the filesystem."""
    agents_dir = get_domain_agents_dir("helm-operator")
    return list_subagents(agents_dirs=[agents_dir])


def _find_meta(metas, name):
    """Find a subagent metadata by name."""
    return next((m for m in metas if m.name == name), None)


def test_discovers_two_subagents(helm_subagent_metas):
    assert len(helm_subagent_metas) == 2


def test_all_subagents_have_required_fields(helm_subagent_metas):
    for meta in helm_subagent_metas:
        assert meta.name, f"Missing name in {meta.path}"
        assert meta.description, f"Missing description in {meta.path}"
        assert meta.system_prompt, f"Missing system_prompt in {meta.path}"


def test_helm_coder_discovered(helm_subagent_metas):
    meta = _find_meta(helm_subagent_metas, "helm-coder")
    assert meta is not None
    assert meta.config.agent_type == "deep"


def test_helm_operation_has_mcp_and_hitl(helm_subagent_metas):
    meta = _find_meta(helm_subagent_metas, "helm-operation")
    assert meta is not None
    mcp_servers = [m.server_name for m in meta.config.tools.mcp]
    assert "helm_mcp_server" in mcp_servers
    
    hitl_spec = next((mw for mw in meta.config.middleware if mw.name == "human_in_the_loop"), None)
    assert hitl_spec is not None
    
    hitl_tools = [t.get("name") if isinstance(t, dict) else t for t in hitl_spec.config.get("tools", [])]
    assert "helm_install_chart" in hitl_tools
    assert "helm_upgrade_release" in hitl_tools
    assert "helm_rollback_release" in hitl_tools
    assert "helm_uninstall_release" in hitl_tools
    
    extra_tools = [t.name for t in meta.config.tools.extra]
    assert "kubectl_readonly" in extra_tools
    assert meta.prompt_composer is None


def test_build_dynamic_spec_produces_valid_dict(helm_subagent_metas):
    """Verify build_dynamic_subagent_spec returns a dict with required keys."""
    for meta in helm_subagent_metas:
        if meta.config.agent_type == "react":
            spec = build_dynamic_subagent_spec(meta)
            assert isinstance(spec, dict)
            assert "name" in spec
            assert "description" in spec
            assert "system_prompt" in spec
