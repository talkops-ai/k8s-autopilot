"""Unit tests for Tools Registry (Phase 12)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.tools import BaseTool


class TestToolRegistry:
    """Tests for ToolRegistry singleton and categorized registration."""

    def test_singleton(self) -> None:
        from k8s_autopilot.tools.registry import get_tool_registry

        r1 = get_tool_registry()
        r2 = get_tool_registry()
        assert r1 is r2

    def test_register_and_get(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        mock_tool = MagicMock(spec=BaseTool)
        registry.register("test_tool", mock_tool, category="test")
        assert registry.get("test_tool") is mock_tool

    def test_get_unknown_returns_none(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        assert registry.get("nonexistent") is None

    def test_list_registered(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("beta", MagicMock(spec=BaseTool), category="a")
        registry.register("alpha", MagicMock(spec=BaseTool), category="b")
        assert registry.list_registered() == ["alpha", "beta"]

    def test_list_by_category(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("shell_cmd", MagicMock(spec=BaseTool), category="shell")
        registry.register("read_file", MagicMock(spec=BaseTool), category="filesystem")
        registry.register("write_file", MagicMock(spec=BaseTool), category="filesystem")

        assert registry.list_by_category("shell") == ["shell_cmd"]
        assert registry.list_by_category("filesystem") == ["read_file", "write_file"]
        assert registry.list_by_category("nonexistent") == []

    def test_get_categories(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("a", MagicMock(spec=BaseTool), category="shell")
        registry.register("b", MagicMock(spec=BaseTool), category="filesystem")
        assert registry.get_categories() == ["filesystem", "shell"]

    def test_build_all(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        t1 = MagicMock(spec=BaseTool)
        t2 = MagicMock(spec=BaseTool)
        registry.register("t1", t1, category="a")
        registry.register("t2", t2, category="b")

        tools = registry.build_all()
        assert len(tools) == 2

    def test_build_all_exclude(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("t1", MagicMock(spec=BaseTool), category="safe")
        registry.register("t2", MagicMock(spec=BaseTool), category="dangerous")

        tools = registry.build_all(exclude_categories={"dangerous"})
        assert len(tools) == 1

    def test_build_all_include(self) -> None:
        from k8s_autopilot.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("t1", MagicMock(spec=BaseTool), category="shell")
        registry.register("t2", MagicMock(spec=BaseTool), category="fs")

        tools = registry.build_all(include_categories={"shell"})
        assert len(tools) == 1
