"""
K8s Autopilot — Shared tools for deep agent subagents.

Provides reusable tool factories that any MCP subagent can opt-in to via
``build_mcp_subagent(extra_tools=[...])``.

Current tools:
    - ``create_kubectl_readonly_tool()`` — scoped kubectl read-only diagnostics
"""
