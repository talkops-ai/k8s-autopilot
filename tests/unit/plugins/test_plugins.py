"""Unit tests for plugin discovery and protocol models."""

from __future__ import annotations

import json
from pathlib import Path

from k8s_autopilot.plugins.discovery import PluginDiscovery, discover_plugins
from k8s_autopilot.plugins.protocol import PluginInstance, PluginManifest


def test_plugin_manifest_and_instance() -> None:
    """Verify PluginManifest and PluginInstance structures."""
    manifest = PluginManifest(
        name="custom-k8s-plugin",
        version="1.2.0",
        description="Custom cluster automation",
    )
    instance = PluginInstance(
        manifest=manifest,
        root_dir=Path("/tmp/custom-k8s-plugin"),
        source="project",
    )

    assert instance.name == "custom-k8s-plugin"
    assert instance.version == "1.2.0"
    assert instance.enabled is True


def test_plugin_discovery_from_directory(tmp_path: Path) -> None:
    """Verify plugin discovery from project plugins directory."""
    plugin_dir = tmp_path / "plugins" / "my-argo-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = plugin_dir / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "my-argo-plugin",
                "version": "2.0.0",
                "description": "ArgoCD automation plugin",
            }
        ),
        encoding="utf-8",
    )

    discovery = PluginDiscovery(project_root=tmp_path)
    plugins = discovery.discover_all()

    assert "my-argo-plugin" in plugins
    plugin = plugins["my-argo-plugin"]
    assert plugin.name == "my-argo-plugin"
    assert plugin.version == "2.0.0"
    assert plugin.manifest.description == "ArgoCD automation plugin"
