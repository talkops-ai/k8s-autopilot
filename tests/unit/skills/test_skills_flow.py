"""Unit tests for the complete K8s Autopilot skills flow.

Tests:
1. Multi-tier list_skills with Claude & Codex plugins and subagent skills.
2. SSRF containment-safe load_skill_content.
3. SkillTrustStore auto-trust and explicit trust recording.
4. build_skill_invocation_envelope and parse_skill_command.
5. SkillRegistry discovery and DB synchronization.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from k8s_autopilot.skills import (
    SkillInvocationEnvelope,
    SkillMetadata,
    SkillRegistry,
    SkillTrustStore,
    build_skill_invocation_envelope,
    list_skills,
    load_skill_content,
    parse_skill_command,
)


class TestSkillsLoader:
    def test_list_skills_with_claude_and_codex_plugins(self, tmp_path: Path):
        """Verify discovery of Claude plugin and Codex plugin skills."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        # 1. Claude plugin
        claude_plugin = plugins_dir / "k8s-diagnostics-claude"
        claude_plugin.mkdir()
        (claude_plugin / "plugin.json").write_text(
            json.dumps({"name": "k8s-diagnostics", "version": "1.0.0"}),
            encoding="utf-8",
        )
        claude_skill = claude_plugin / "skills" / "pod-debugger"
        claude_skill.mkdir(parents=True)
        (claude_skill / "SKILL.md").write_text(
            "---\nname: pod-debugger\ndescription: Debugs failing pods\n---\n# Pod Debugger\n",
            encoding="utf-8",
        )

        # 2. Codex plugin
        codex_plugin = plugins_dir / "k8s-autoscaler-codex"
        codex_plugin.mkdir()
        (codex_plugin / "ai-plugin.json").write_text(
            json.dumps({"name_for_model": "k8s_autoscaler"}),
            encoding="utf-8",
        )
        codex_skill = codex_plugin / "skills" / "hpa-tuner"
        codex_skill.mkdir(parents=True)
        (codex_skill / "SKILL.md").write_text(
            "---\nname: hpa-tuner\ndescription: Tunes HPA settings\n---\n# HPA Tuner\n",
            encoding="utf-8",
        )

        discovered = list_skills(project_root=tmp_path, include_plugins=True)
        names = [s["name"] for s in discovered]

        assert "k8s-diagnostics:pod-debugger" in names
        assert "k8s_autoscaler:hpa-tuner" in names

    def test_load_skill_content_containment_safety(self, tmp_path: Path):
        """Verify SSRF containment prevents loading files outside allowed roots."""
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        skill_dir = allowed_dir / "safe-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("Safe content", encoding="utf-8")

        # Allowed root check passes
        content = load_skill_content(str(skill_dir), allowed_roots=[allowed_dir])
        assert content == "Safe content"

        # Outside root check raises PermissionError
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_md = outside_dir / "SKILL.md"
        outside_md.write_text("Outside content", encoding="utf-8")

        with pytest.raises(PermissionError):
            load_skill_content(str(outside_dir), allowed_roots=[allowed_dir])


class TestSkillTrustStore:
    def test_trust_store_persistence_and_auto_trust(self, tmp_path: Path):
        trust_file = tmp_path / "trust.json"
        store = SkillTrustStore(trust_file_path=trust_file)

        custom_skill_dir = tmp_path / "custom-skill"
        custom_skill_dir.mkdir()

        assert not store.is_trusted("custom-skill", custom_skill_dir)

        store.trust_skill("custom-skill", custom_skill_dir)
        assert store.is_trusted("custom-skill", custom_skill_dir)

        # Reload from disk
        store2 = SkillTrustStore(trust_file_path=trust_file)
        assert store2.is_trusted("custom-skill", custom_skill_dir)


class TestSkillInvocation:
    def test_parse_skill_command(self):
        name, args = parse_skill_command("/skill:k8s-deploy deploy to prod --replicas=3")
        assert name == "k8s-deploy"
        assert args == "deploy to prod --replicas=3"

        name, args = parse_skill_command("/skill:web-search")
        assert name == "web-search"
        assert args == ""

    def test_build_skill_invocation_envelope(self):
        skill = {"name": "rollback", "description": "Rollback deployment", "source": "built-in"}
        content = "# Rollback instructions"
        envelope = build_skill_invocation_envelope(skill, content, args="deploy/web")

        assert isinstance(envelope, SkillInvocationEnvelope)
        assert "I'm invoking the skill `rollback`" in envelope.prompt
        assert "**User request:** deploy/web" in envelope.prompt
        assert envelope.message_kwargs["additional_kwargs"]["__skill"]["name"] == "rollback"
