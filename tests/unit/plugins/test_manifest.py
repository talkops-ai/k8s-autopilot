"""Unit tests for plugin manifest parsing and security validations."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from k8s_autopilot.plugins.manifest import (
    PluginManifestError,
    build_inventory,
    load_manifest,
)


def test_load_manifest_valid(tmp_path: Path) -> None:
    """Test loading valid plugin.json manifest."""
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "helm-guard",
                "version": "1.1.0",
                "displayName": "Helm Guard",
                "description": "Validates helm charts before deploy",
                "skills": ["./skills"],
                "mcpServers": {
                    "helm-mcp": {
                        "command": "python",
                        "args": ["-m", "helm_mcp"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    manifest, path, warnings = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.name == "helm-guard"
    assert manifest.version == "1.1.0"
    assert manifest.display_name == "Helm Guard"
    assert manifest.description == "Validates helm charts before deploy"
    assert "skills" in manifest.component_paths
    assert "helm-mcp" in manifest.inline_mcp


def test_load_manifest_path_traversal_prevention(tmp_path: Path) -> None:
    """Test that path traversal attempts in component declarations are rejected."""
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "malicious-plugin",
                "skills": ["../escaped_dir"],
                "agents": ["/absolute/path"],
            }
        ),
        encoding="utf-8",
    )

    manifest, path, warnings = load_manifest(tmp_path)
    assert manifest is not None
    assert len(warnings) >= 2
    assert any("must start with './'" in w or "path must not contain '..'" in w for w in warnings)
    assert "skills" not in manifest.component_paths
    assert "agents" not in manifest.component_paths


def test_build_inventory_discovers_defaults(tmp_path: Path) -> None:
    """Test default discovery of skills, agents, commands, and mcp configs."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "my_skill").mkdir()
    (skills_dir / "my_skill" / "SKILL.md").write_text("Skill documentation")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text("{}")

    inventory = build_inventory(tmp_path, None)
    assert len(inventory.skills) >= 1
    assert len(inventory.agents) >= 1
    assert len(inventory.mcp_files) >= 1
