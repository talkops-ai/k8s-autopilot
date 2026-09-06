"""Unit tests for subagent loader, parser, and middleware — directory scanning, YAML parsing, priority merging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class TestSubagentFileParsing:
    def _create_agent_file(self, tmp_path: Path, name: str, content: str) -> Path:
        agent_dir = tmp_path / name
        agent_dir.mkdir()
        agent_file = agent_dir / "AGENTS.md"
        agent_file.write_text(content, encoding="utf-8")
        return agent_file

    def test_valid_frontmatter(self, tmp_path):
        from k8s_autopilot.subagents.loader import _parse_subagent_file

        f = self._create_agent_file(
            tmp_path, "agent",
            "---\nname: agent\ndescription: An agent\n---\nYou are an agent.",
        )
        meta = _parse_subagent_file(f)
        assert meta is not None
        assert meta["name"] == "agent"
        assert meta["description"] == "An agent"
        assert meta["system_prompt"] == "You are an agent."

    def test_missing_frontmatter(self, tmp_path):
        from k8s_autopilot.subagents.loader import _parse_subagent_file

        f = self._create_agent_file(tmp_path, "bad", "No frontmatter.")
        assert _parse_subagent_file(f) is None

    def test_missing_description(self, tmp_path):
        from k8s_autopilot.subagents.loader import _parse_subagent_file

        f = self._create_agent_file(tmp_path, "nd", "---\nname: nd\n---\nPrompt.")
        assert _parse_subagent_file(f) is None

    def test_fallback_name_from_folder(self, tmp_path):
        from k8s_autopilot.subagents.loader import _parse_subagent_file

        f = self._create_agent_file(
            tmp_path, "folder-name",
            "---\ndescription: Has desc\n---\nPrompt.",
        )
        meta = _parse_subagent_file(f, fallback_name="folder-name")
        assert meta is not None
        assert meta["name"] == "folder-name"

    def test_skills_and_tools_parsed(self, tmp_path):
        from k8s_autopilot.subagents.loader import _parse_subagent_file

        f = self._create_agent_file(
            tmp_path, "skilled",
            "---\nname: skilled\ndescription: Has skills\nskills:\n  - a\n  - b\ntools:\n  - x\n---\nP.",
        )
        meta = _parse_subagent_file(f)
        assert meta is not None
        assert meta["skills"] == ["a", "b"]
        assert meta["tools"] == ["x"]


class TestSubagentDirectoryLoading:
    def _create_agent_file(self, tmp_path: Path, name: str, content: str) -> Path:
        agent_dir = tmp_path / name
        agent_dir.mkdir()
        (agent_dir / "AGENTS.md").write_text(content, encoding="utf-8")
        return agent_dir

    def test_load_multiple_agents(self, tmp_path):
        from k8s_autopilot.subagents.loader import _load_subagents_from_dir

        self._create_agent_file(
            tmp_path, "a", "---\nname: a\ndescription: A\n---\nPrompt A.",
        )
        self._create_agent_file(
            tmp_path, "b", "---\nname: b\ndescription: B\n---\nPrompt B.",
        )
        result = _load_subagents_from_dir(tmp_path, "test")
        assert len(result) == 2
        assert "a" in result and "b" in result

    def test_nonexistent_dir(self, tmp_path):
        from k8s_autopilot.subagents.loader import _load_subagents_from_dir

        assert _load_subagents_from_dir(tmp_path / "nonexistent", "test") == {}


class TestSubagentPriorityMerging:
    def _create_agent_file(self, tmp_path: Path, name: str, desc: str) -> Path:
        agent_dir = tmp_path / name
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "AGENTS.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nPrompt.", encoding="utf-8",
        )
        return agent_dir

    def test_project_overrides_user(self, tmp_path):
        from k8s_autopilot.subagents.loader import list_subagents

        user_dir = tmp_path / "user"
        user_dir.mkdir()
        proj_dir = tmp_path / "project"
        proj_dir.mkdir()

        self._create_agent_file(user_dir, "shared", "User version")
        self._create_agent_file(proj_dir, "shared", "Project version")

        result = list_subagents(user_agents_dir=user_dir, project_agents_dir=proj_dir)
        shared = [m for m in result if m["name"] == "shared"]
        assert len(shared) == 1
        assert shared[0]["description"] == "Project version"


class TestSubagentsMiddleware:
    def test_empty_registry(self):
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        mw = SubagentsMiddleware()
        assert mw.subagent_names == []
        assert mw._build_prompt_block() == ""

    def test_registration_and_lookup(self):
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        mw = SubagentsMiddleware()
        mw.register_subagent({
            "name": "agent-x",
            "description": "Agent X",
            "system_prompt": "Prompt",
            "source": "user",
            "path": "/agents/x/AGENTS.md",
        })
        assert mw.get_subagent("agent-x") is not None
        assert mw.get_subagent("nonexistent") is None

    def test_prompt_block_content(self):
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        mw = SubagentsMiddleware(subagent_metas=[{
            "name": "reviewer",
            "description": "Code reviewer",
            "system_prompt": "...",
            "source": "project",
            "path": "/agents/r/AGENTS.md",
        }])
        block = mw._build_prompt_block()
        assert "reviewer" in block
        assert "Code reviewer" in block
        assert "Subagent Delegation" in block

    def test_plugin_subagent_classification(self):
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        mw = SubagentsMiddleware(subagent_metas=[
            {
                "name": "builtin", "description": "Built-in",
                "system_prompt": "...", "source": "project",
                "path": "/agents/b/AGENTS.md",
            },
            {
                "name": "ext@plugin", "description": "Plugin ext",
                "system_prompt": "...", "source": "plugin",
                "path": "/plugins/p/agents/e/AGENTS.md",
                "is_plugin": True,
            },
        ])
        block = mw._build_prompt_block()
        assert "Built-in Subagents" in block
        assert "Plugin & Extension Subagents" in block
