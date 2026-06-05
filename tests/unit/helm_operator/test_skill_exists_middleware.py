"""Tests for the ``skill_exists_shortcut`` middleware and ``_apply_skill_shortcut``.

Verifies:
  1. ``_SKILL_PATTERN`` regex matches expected skill paths and rejects others.
  2. ``_apply_skill_shortcut`` passes through when no skills exist in state.
  3. When skills exist:
     a. ``helm-planner`` and ``helm-skill-builder`` are removed from tools.
     b. A ``[SKILL-EXISTS SHORTCUT]`` directive ``SystemMessage`` is injected.
     c. Other tools (``helm-generator``, ``helm-validator``) are preserved.
  4. The logic is stateless (re-checks ``state["files"]`` on every call).
  5. ``skill_exists_shortcut`` is a valid LangChain middleware object.

Reference
---------
- LangChain: Filtering pre-registered tools
  https://docs.langchain.com/oss/python/langchain/tools#filtering-pre-registered-tools
- LangChain: Dynamic prompt via @wrap_model_call
  https://docs.langchain.com/oss/python/langchain/middleware/custom#dynamic-prompt
"""

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import SystemMessage, HumanMessage

from k8s_autopilot.core.agents.helm_operator.middleware import (
    _SKILL_PATTERN,
    _SKIP_WHEN_SKILLS_EXIST,
    _apply_skill_shortcut,
    skill_exists_shortcut,
    SkillExistsMiddleware,
)


# ---------------------------------------------------------------------------
# Regex pattern tests
# ---------------------------------------------------------------------------


class TestSkillPattern:
    """Verify ``_SKILL_PATTERN`` matches the expected skill file paths."""

    @pytest.mark.parametrize(
        "path",
        [
            "/skills/helm-operator/nginx-chart-generator/SKILL.md",
            "/skills/helm-operator/payment-service-chart-generator/SKILL.md",
            "/skills/helm-operator/my-app-chart-generator/SKILL.md",
            "/skills/helm-operator/redis-chart-generator/SKILL.md",
            "/skills/helm-operator/postgres_db-chart-generator/SKILL.md",
        ],
    )
    def test_matches_valid_skill_paths(self, path: str):
        assert _SKILL_PATTERN.match(path), f"Should match: {path}"

    @pytest.mark.parametrize(
        "path,reason",
        [
            (
                "/skills/helm-operator/helm-generator/SKILL.md",
                "generic generator skill, not app-specific chart-generator",
            ),
            (
                "/skills/app-operator/nginx-chart-generator/SKILL.md",
                "wrong operator prefix (app-operator, not helm-operator)",
            ),
            (
                "/skills/helm-operator/nginx-chart-generator/README.md",
                "wrong filename (README.md, not SKILL.md)",
            ),
            (
                "/skills/helm-operator/nginx/SKILL.md",
                "missing -chart-generator suffix",
            ),
            (
                "/workspace/helm-operator/nginx-chart-generator/SKILL.md",
                "wrong root directory (workspace, not skills)",
            ),
            (
                "/skills/helm-operator/SKILL.md",
                "missing app directory",
            ),
        ],
    )
    def test_rejects_invalid_paths(self, path: str, reason: str):
        assert not _SKILL_PATTERN.match(path), f"Should NOT match ({reason}): {path}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str) -> MagicMock:
    """Create a mock tool with a ``name`` attribute."""
    tool = MagicMock()
    tool.name = name
    return tool


ALL_TOOLS = [
    "helm-planner",
    "helm-skill-builder",
    "helm-generator",
    "helm-validator",
    "read_file",
    "write_file",
]

NGINX_SKILL = "/skills/helm-operator/nginx-chart-generator/SKILL.md"


# ---------------------------------------------------------------------------
# _apply_skill_shortcut — passthrough tests
# ---------------------------------------------------------------------------


class TestApplySkillShortcutPassthrough:
    """When no skills exist, the function should pass through unmodified."""

    def test_empty_files(self):
        """No files at all → no activation."""
        tools = [_make_tool(n) for n in ALL_TOOLS]
        messages = [HumanMessage(content="Create nginx chart")]
        state = {"messages": messages, "files": {}}

        out_tools, out_msgs, activated = _apply_skill_shortcut(state, tools, messages)

        assert not activated
        assert out_tools is tools  # same object (not copied)
        assert out_msgs is messages

    def test_unrelated_files(self):
        """Files exist but don't match the skill pattern → no activation."""
        files = {
            "/workspace/Chart.yaml": {"content": "apiVersion: v2"},
            "/skills/helm-operator/helm-generator/SKILL.md": {"content": "..."},
            "/memory/operations-log.md": {"content": "..."},
        }
        tools = [_make_tool(n) for n in ALL_TOOLS]
        messages = []
        state = {"messages": messages, "files": files}

        _, _, activated = _apply_skill_shortcut(state, tools, messages)

        assert not activated

    def test_state_without_files_key(self):
        """State dict has no 'files' key → pass through gracefully."""
        tools = [_make_tool(n) for n in ALL_TOOLS]
        messages = []
        state = {"messages": []}  # No 'files' key

        _, _, activated = _apply_skill_shortcut(state, tools, messages)

        assert not activated

    def test_non_dict_state(self):
        """Non-dict state (edge case) → no crash."""
        tools = [_make_tool(n) for n in ALL_TOOLS]

        # MagicMock won't be isinstance(dict), so should return early
        state = MagicMock()
        _, _, activated = _apply_skill_shortcut(state, tools, [])

        assert not activated


# ---------------------------------------------------------------------------
# _apply_skill_shortcut — activation tests
# ---------------------------------------------------------------------------


class TestApplySkillShortcutActivation:
    """When matching skills exist, tools should be filtered and directive injected."""

    def test_removes_planner_and_skill_builder(self):
        """Planner and skill-builder should be removed from tools."""
        tools = [_make_tool(n) for n in ALL_TOOLS]
        files = {NGINX_SKILL: {"content": "# nginx skill"}}
        state = {"files": files}

        out_tools, _, activated = _apply_skill_shortcut(state, tools, [])

        assert activated
        tool_names = {t.name for t in out_tools}
        assert "helm-planner" not in tool_names
        assert "helm-skill-builder" not in tool_names

    def test_preserves_other_tools(self):
        """Non-planner tools should remain available."""
        tools = [_make_tool(n) for n in ALL_TOOLS]
        files = {NGINX_SKILL: {"content": "# nginx skill"}}
        state = {"files": files}

        out_tools, _, activated = _apply_skill_shortcut(state, tools, [])

        assert activated
        tool_names = {t.name for t in out_tools}
        assert "helm-generator" in tool_names
        assert "helm-validator" in tool_names
        assert "read_file" in tool_names
        assert "write_file" in tool_names

    def test_tool_count_reduced(self):
        """Two tools should be removed (planner + skill-builder)."""
        tools = [_make_tool(n) for n in ALL_TOOLS]
        files = {NGINX_SKILL: {"content": "# nginx"}}
        state = {"files": files}

        out_tools, _, _ = _apply_skill_shortcut(state, tools, [])

        assert len(out_tools) == len(ALL_TOOLS) - 2  # minus planner, skill-builder

    def test_injects_directive_system_message(self):
        """A SKILL-EXISTS SHORTCUT SystemMessage should be appended."""
        files = {NGINX_SKILL: {"content": "# nginx skill"}}
        state = {"files": files}
        original_messages = [HumanMessage(content="Create nginx chart")]

        _, out_msgs, activated = _apply_skill_shortcut(state, [], original_messages)

        assert activated
        assert len(out_msgs) == 2  # original + directive
        directive = out_msgs[-1]
        assert isinstance(directive, SystemMessage)
        assert "[SKILL-EXISTS SHORTCUT]" in directive.content
        assert "helm-planner" in directive.content
        assert "helm-generator" in directive.content

    def test_directive_contains_skill_path(self):
        """Directive message should include the matched skill file path."""
        files = {NGINX_SKILL: {"content": "# nginx skill"}}
        state = {"files": files}

        _, out_msgs, _ = _apply_skill_shortcut(state, [], [])

        directive = out_msgs[-1]
        assert NGINX_SKILL in directive.content

    def test_does_not_mutate_original_messages(self):
        """Original message list should not be modified (new list returned)."""
        files = {NGINX_SKILL: {"content": "..."}}
        state = {"files": files}
        original = [HumanMessage(content="hello")]

        _, out_msgs, _ = _apply_skill_shortcut(state, [], original)

        assert len(original) == 1  # unchanged
        assert len(out_msgs) == 2  # original + directive

    def test_multiple_matching_skills(self):
        """Multiple skill files → all listed in directive, tools still filtered."""
        files = {
            NGINX_SKILL: {"content": "# nginx"},
            "/skills/helm-operator/redis-chart-generator/SKILL.md": {"content": "# redis"},
        }
        tools = [_make_tool(n) for n in ALL_TOOLS]
        state = {"files": files}

        out_tools, out_msgs, activated = _apply_skill_shortcut(state, tools, [])

        assert activated

        directive = out_msgs[-1].content
        assert "nginx-chart-generator" in directive
        assert "redis-chart-generator" in directive

        tool_names = {t.name for t in out_tools}
        assert "helm-planner" not in tool_names
        assert "helm-skill-builder" not in tool_names


# ---------------------------------------------------------------------------
# Statelessness tests
# ---------------------------------------------------------------------------


class TestApplySkillShortcutStatelessness:
    """The function is stateless — each call evaluates independently."""

    def test_first_call_no_skills_second_call_has_skills(self):
        """Two calls with different states produce different results."""
        tools = [_make_tool(n) for n in ALL_TOOLS]

        # Call 1: no skills → pass through
        state1 = {"files": {}}
        _, _, activated1 = _apply_skill_shortcut(state1, tools, [])
        assert not activated1

        # Call 2: skills exist → activate
        state2 = {"files": {NGINX_SKILL: {"content": "..."}}}
        _, _, activated2 = _apply_skill_shortcut(state2, tools, [])
        assert activated2

    def test_skills_appear_then_disappear(self):
        """Simulates skill removal between calls."""
        tools = [_make_tool(n) for n in ALL_TOOLS]

        # Call 1: skills exist → activate
        state1 = {"files": {NGINX_SKILL: {"content": "..."}}}
        _, _, activated1 = _apply_skill_shortcut(state1, tools, [])
        assert activated1

        # Call 2: skills removed → pass through
        state2 = {"files": {}}
        _, _, activated2 = _apply_skill_shortcut(state2, tools, [])
        assert not activated2


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestSkipWhenSkillsExistConstants:
    """Verify the constant set contains exactly the expected sub-agents."""

    def test_skip_set(self):
        assert _SKIP_WHEN_SKILLS_EXIST == {"helm-planner", "helm-skill-builder"}

    def test_is_frozen(self):
        assert isinstance(_SKIP_WHEN_SKILLS_EXIST, frozenset)


# ---------------------------------------------------------------------------
# Middleware object type tests
# ---------------------------------------------------------------------------


class TestSkillExistsShortcutMiddleware:
    """Verify ``skill_exists_shortcut`` is a valid LangChain middleware object."""

    def test_is_middleware_instance(self):
        """Should be an instance of AgentMiddleware."""
        from langchain.agents.middleware import AgentMiddleware

        assert isinstance(skill_exists_shortcut, AgentMiddleware)

    def test_is_skill_exists_middleware_instance(self):
        """Should be an instance of our SkillExistsMiddleware class."""
        assert isinstance(skill_exists_shortcut, SkillExistsMiddleware)

    def test_has_wrap_model_call_method(self):
        """Should have the sync wrap_model_call method."""
        assert hasattr(skill_exists_shortcut, "wrap_model_call")
        assert callable(skill_exists_shortcut.wrap_model_call)

    def test_has_awrap_model_call_method(self):
        """Should have the async awrap_model_call method — required for ainvoke()."""
        assert hasattr(skill_exists_shortcut, "awrap_model_call")
        assert callable(skill_exists_shortcut.awrap_model_call)
