"""Unit tests for Prompts System."""

from __future__ import annotations

import re

import pytest


class TestSystemPrompt:
    """Tests for system prompt builder."""

    def test_get_base_system_prompt(self) -> None:
        from k8s_autopilot.prompts.system import get_base_system_prompt

        prompt = get_base_system_prompt(
            model_name="gpt-4o",
            model_provider="openai",
            model_context_limit=128_000,
        )
        assert "K8s Autopilot" in prompt
        assert "gpt-4o" in prompt
        assert "openai" in prompt
        assert "128,000" in prompt
        assert "Core Deep-Agent Paradigm" in prompt
        assert "Kubernetes & Cloud-Native Platform Engineering Conventions" in prompt

    def test_model_identity_section(self) -> None:
        from k8s_autopilot.prompts.system import build_model_identity_section

        section = build_model_identity_section(
            name="gemini-2.5-flash",
            provider="google_genai",
            context_limit=1_000_000,
        )
        assert "### Model Identity" in section
        assert "gemini-2.5-flash" in section
        assert "google_genai" in section
        assert "1,000,000" in section

    def test_model_identity_no_context_limit(self) -> None:
        from k8s_autopilot.prompts.system import build_model_identity_section

        section = build_model_identity_section(
            name="claude-3-5-sonnet",
            provider="anthropic",
        )
        assert "### Model Identity" in section
        assert "context window" not in section

    def test_model_identity_regex(self) -> None:
        from k8s_autopilot.prompts import MODEL_IDENTITY_RE

        text = "Before\n### Model Identity\n\nYou are running as model `test`.\n\n### Next Section\nAfter"
        match = MODEL_IDENTITY_RE.search(text)
        assert match is not None
        assert "test" in match.group(0)

    def test_fallback_prompt_used(self) -> None:
        from k8s_autopilot.prompts.system import _FALLBACK_SYSTEM_PROMPT

        assert "K8s Autopilot" in _FALLBACK_SYSTEM_PROMPT
        assert "Kubernetes" in _FALLBACK_SYSTEM_PROMPT


class TestSystemPromptTemplate:
    """Tests for the system prompt template file."""

    def test_template_exists(self) -> None:
        from pathlib import Path

        template = (
            Path(__file__).parent.parent.parent
            / "k8s_autopilot"
            / "prompts"
            / "templates"
            / "system_prompt.md"
        )
        assert template.exists()

    def test_template_has_key_sections(self) -> None:
        from pathlib import Path

        template = (
            Path(__file__).parent.parent.parent
            / "k8s_autopilot"
            / "prompts"
            / "templates"
            / "system_prompt.md"
        )
        content = template.read_text()
        assert "Core Deep-Agent Paradigm" in content
        assert "Communication & Behavioral Protocols" in content
        assert "Kubernetes & Cloud-Native Platform Engineering Conventions" in content
        assert "Safety, Git & Security Protocols" in content
        assert "Anti-Looping Circuit Breaker" in content
        assert "Stateful Planning & Goal-Driven Execution" in content

    def test_get_base_system_prompt_includes_goal_planning(self) -> None:
        from k8s_autopilot.prompts.system import get_base_system_prompt

        prompt = get_base_system_prompt()
        assert "propose_goal" in prompt
        assert "Stateful Planning & Goal-Driven Execution" in prompt
        assert "write_todos" in prompt


class TestSREMemorySystemPrompt:
    """Tests for SRE memory system prompt used by MemoryMiddleware."""

    def test_sre_memory_prompt_format_and_placeholders(self) -> None:
        from k8s_autopilot.prompts import SRE_MEMORY_SYSTEM_PROMPT

        assert "{agent_memory}" in SRE_MEMORY_SYSTEM_PROMPT
        assert "<agent_memory>" in SRE_MEMORY_SYSTEM_PROMPT
        assert "</agent_memory>" in SRE_MEMORY_SYSTEM_PROMPT
        assert "<memory_guidelines>" in SRE_MEMORY_SYSTEM_PROMPT
        assert "</memory_guidelines>" in SRE_MEMORY_SYSTEM_PROMPT

    def test_sre_memory_prompt_rules(self) -> None:
        from k8s_autopilot.prompts import SRE_MEMORY_SYSTEM_PROMPT

        # SRE specific guidelines
        assert "Trust and Verification" in SRE_MEMORY_SYSTEM_PROMPT
        assert "Information Hygiene" in SRE_MEMORY_SYSTEM_PROMPT
        assert "When to Update Memory" in SRE_MEMORY_SYSTEM_PROMPT
        assert "When NOT to Update Memory" in SRE_MEMORY_SYSTEM_PROMPT
        assert "AGENTS.md" in SRE_MEMORY_SYSTEM_PROMPT

        # Ensure consumer chat narratives are eliminated
        assert "basketball" not in SRE_MEMORY_SYSTEM_PROMPT
        assert "google account" not in SRE_MEMORY_SYSTEM_PROMPT
        assert "calendar" not in SRE_MEMORY_SYSTEM_PROMPT
        assert "recipe" not in SRE_MEMORY_SYSTEM_PROMPT

    def test_memory_middleware_accepts_sre_prompt(self) -> None:
        from deepagents.backends.filesystem import FilesystemBackend
        from deepagents.middleware.memory import MemoryMiddleware
        from k8s_autopilot.prompts import SRE_MEMORY_SYSTEM_PROMPT

        mw = MemoryMiddleware(
            backend=FilesystemBackend(virtual_mode=False),
            sources=["/tmp/test_agents.md"],
            system_prompt=SRE_MEMORY_SYSTEM_PROMPT,
        )
        assert mw.system_prompt == SRE_MEMORY_SYSTEM_PROMPT
        formatted = mw._format_agent_memory(
            {"/tmp/test_agents.md": "# Cluster Guidelines\nAlways use internal ingress"},
            mw.system_prompt,
        )
        assert "Always use internal ingress" in formatted
        assert "Trust and Verification" in formatted
        assert "basketball" not in formatted


