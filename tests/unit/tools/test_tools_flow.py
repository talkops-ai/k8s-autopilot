"""Unit tests for the K8s Autopilot tools subsystem."""

from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableConfig

from k8s_autopilot.tools import (
    ToolRegistry,
    fetch_url,
    format_json_output,
    format_tool_error,
    format_tool_success,
    get_current_thread_id,
    get_goal,
    get_rubric,
    register_all_tools,
    update_goal,
    web_search,
)


class TestDisplayHelpers:
    def test_format_tool_error(self):
        msg = format_tool_error("Failed to apply manifest")
        assert "Error: Failed to apply manifest" in msg

    def test_format_tool_success(self):
        msg = format_tool_success("Cluster healthy")
        assert "Success: Cluster healthy" in msg

    def test_format_json_output(self):
        out = format_json_output({"status": "ready", "replicas": 3})
        assert '"status": "ready"' in out


class TestThreadTool:
    def test_get_current_thread_id(self):
        config: RunnableConfig = {"configurable": {"thread_id": "thread-123"}}
        res = get_current_thread_id.invoke({}, config=config)
        assert res == "thread-123"

        config_empty: RunnableConfig = {}
        res2 = get_current_thread_id.invoke({}, config=config_empty)
        assert "No current thread ID" in res2


class TestToolRegistryAndCatalog:
    def test_register_and_build_all(self):
        register_all_tools()
        registry = ToolRegistry.get_instance()

        registered = registry.list_registered()
        assert "web_search" in registered
        assert "fetch_url" in registered
        assert "get_current_thread_id" in registered
        assert "get_rubric" in registered
        assert "get_goal" in registered
        assert "update_goal" in registered

        # Verify filesystem tools are NOT in ToolRegistry (middleware-managed)
        for fs_tool in ["read_file", "write_file", "edit_file", "delete_file", "execute"]:
            assert fs_tool not in registered

        tool = registry.build_tool("web_search")
        assert tool.name == "web_search"

