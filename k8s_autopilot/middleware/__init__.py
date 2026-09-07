"""Middleware stack for K8s Autopilot agent.

All middleware implement the AgentMiddleware protocol from LangChain:
- before_agent / abefore_agent
- before_model / abefore_model
- wrap_model_call / awrap_model_call
- after_model / aafter_model
- after_agent / aafter_agent

Middleware are applied in ORDER. The order defined in agent/factory.py
is the execution order.
"""


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Lazy-load middleware classes to avoid circular imports."""
    _lazy_map = {
        "MiddlewareRegistry": "k8s_autopilot.middleware.registry",
        "get_middleware_registry": "k8s_autopilot.middleware.registry",
        "ResumeStateMiddleware": "k8s_autopilot.middleware.resume_state",
        "ConfigurableModelMiddleware": "k8s_autopilot.middleware.configurable_model",
        "UnifiedSystemMessageMiddleware": "k8s_autopilot.middleware.unified_system_message",
        "LocalContextMiddleware": "k8s_autopilot.middleware.local_context",
        "PluginSkillsMiddleware": "k8s_autopilot.middleware.skills",
        "SubagentsMiddleware": "k8s_autopilot.middleware.subagents",
        "GoalToolsMiddleware": "k8s_autopilot.middleware.goal_tools",
        "AskUserMiddleware": "k8s_autopilot.middleware.ask_user",
        "AutoModeHITLMiddleware": "k8s_autopilot.middleware.auto_mode_hitl",
        "AsyncApprovalHITLMiddleware": "k8s_autopilot.middleware.auto_mode",
        "GoalCriteriaMiddleware": "k8s_autopilot.middleware.goal_criteria",
        "ManagedMemoryGuardMiddleware": "k8s_autopilot.middleware.memory_guard",
        "CompactionMiddleware": "k8s_autopilot.middleware.compaction",
        "CLICompactionMiddleware": "k8s_autopilot.middleware.compaction",
        "ReliableRubricMiddleware": "k8s_autopilot.middleware.reliable_rubric",
        "ShellAllowListMiddleware": "k8s_autopilot.middleware.shell_allow_list",
        "ToolFilterMiddleware": "k8s_autopilot.middleware.tool_filter",
        "CostTrackingMiddleware": "k8s_autopilot.middleware.cost_tracking",
        "ServerHooksMiddleware": "k8s_autopilot.middleware.server_hooks",
        "HeadlessMCPGuardMiddleware": "k8s_autopilot.middleware.headless_mcp_guard",
        "GlmTerminalStallRecoveryMiddleware": "k8s_autopilot.middleware.glm_stall_recovery",
    }
    if name in _lazy_map:
        import importlib

        mod = importlib.import_module(_lazy_map[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
