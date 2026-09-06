"""Unit tests for plugin and marketplace data models."""

from __future__ import annotations

from pathlib import Path
import pytest

from k8s_autopilot.plugins.models import (
    ComponentInventory,
    InstalledPluginEntry,
    MarketplacePluginEntry,
    MarketplaceRecord,
    PluginDiscoveryResult,
    PluginInstance,
    PluginManifest,
    PluginMarketplace,
    LocalPluginSource,
    GithubPluginSource,
    namespaced_skill_name,
    split_plugin_id,
)


def test_split_plugin_id() -> None:
    """Test splitting plugin ids."""
    p_name, m_name = split_plugin_id("terraform-linter@devops-toolkit")
    assert p_name == "terraform-linter"
    assert m_name == "devops-toolkit"

    # Single name defaults to "default" marketplace
    p_name2, m_name2 = split_plugin_id("local-plugin")
    assert p_name2 == "local-plugin"
    assert m_name2 == "default"


def test_namespaced_skill_name() -> None:
    """Test qualifying skill names under a namespace."""
    name = namespaced_skill_name("devops-toolkit", "lint_chart", ("helm",))
    assert name == "devops-toolkit:helm:lint_chart"


def test_plugin_instance_validation() -> None:
    """Test PluginInstance creation and id matching."""
    manifest = PluginManifest(
        name="k8s-debugger",
        version="1.5.0",
        display_name="K8s Debugger",
        description="Diagnose crashing pods",
    )
    instance = PluginInstance(
        plugin_id="k8s-debugger@community",
        name="k8s-debugger",
        marketplace="community",
        version="1.5.0",
        root=Path("/tmp/k8s-debugger"),
        manifest=manifest,
    )
    assert instance.plugin_id == "k8s-debugger@community"
    assert instance.root_dir == Path("/tmp/k8s-debugger")
    assert instance.manifest.display_name == "K8s Debugger"

    with pytest.raises(ValueError, match="does not match expected"):
        PluginInstance(
            plugin_id="wrong-name@community",
            name="k8s-debugger",
            marketplace="community",
            root=Path("/tmp/k8s-debugger"),
        )


def test_marketplace_models() -> None:
    """Test MarketplacePluginEntry and PluginMarketplace models."""
    entry = MarketplacePluginEntry(
        name="argo-tool",
        source=GithubPluginSource(source_type="github", repo="org/argo-tool"),
        description="ArgoCD tools",
    )
    marketplace = PluginMarketplace(
        name="devops-catalog",
        root=Path("/tmp/marketplaces/devops"),
        manifest_path=Path("/tmp/marketplaces/devops/marketplace.json"),
        metadata={"pluginRoot": "./plugins"},
        plugins=(entry,),
    )
    assert marketplace.name == "devops-catalog"
    assert len(marketplace.plugins) == 1
    assert marketplace.plugins[0].name == "argo-tool"
