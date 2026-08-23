"""Unit tests to verify discovery, registration, and instantiation of ported middlewares."""

import pytest
from unittest.mock import MagicMock
from langchain.agents.middleware import AgentMiddleware

from k8s_autopilot.core.middleware.registry import get_middleware_registry


@pytest.mark.unit
def test_all_ported_middlewares_are_registered():
    registry = get_middleware_registry()
    
    # Assert that all 7 ported middlewares are present in the registry
    expected_names = [
        "local_context",
        "shell_allow_list",
        "goal_tools",
        "ask_user",
        "configurable_model",
        "resume_state",
        "memory_guard",
    ]
    for name in expected_names:
        assert name in registry._registry, f"Middleware '{name}' should be registered."


@pytest.mark.unit
def test_instantiate_local_context():
    registry = get_middleware_registry()
    instance = registry.build_middleware("local_context", backend=MagicMock())
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "LocalContextMiddleware"


@pytest.mark.unit
def test_instantiate_shell_allow_list():
    registry = get_middleware_registry()
    instance = registry.build_middleware("shell_allow_list", allow_list=["ls", "cat"])
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "ShellAllowListMiddleware"


@pytest.mark.unit
def test_instantiate_goal_tools():
    registry = get_middleware_registry()
    instance = registry.build_middleware("goal_tools")
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "GoalToolsMiddleware"


@pytest.mark.unit
def test_instantiate_ask_user():
    registry = get_middleware_registry()
    instance = registry.build_middleware("ask_user")
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "AskUserMiddleware"


@pytest.mark.unit
def test_instantiate_configurable_model():
    registry = get_middleware_registry()
    instance = registry.build_middleware("configurable_model")
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "ConfigurableModelMiddleware"


@pytest.mark.unit
def test_instantiate_resume_state():
    registry = get_middleware_registry()
    instance = registry.build_middleware("resume_state")
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "ResumeStateMiddleware"


@pytest.mark.unit
def test_instantiate_memory_guard():
    registry = get_middleware_registry()
    instance = registry.build_middleware("memory_guard", guarded_paths=["/path/to/AGENTS.md"])
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "ManagedMemoryGuardMiddleware"


@pytest.mark.unit
def test_base_delegating_middleware_delegation():
    from k8s_autopilot.core.middleware.registry import BaseDelegatingMiddleware
    
    class MockInnerMiddleware:
        state_schema = "MockStateSchema"
        tools = ["MockTool1", "MockTool2"]
        transformers = ("MockTransformer1",)
        name = "MockInnerName"
        
    inner = MockInnerMiddleware()
    delegating = BaseDelegatingMiddleware(inner)
    
    # Assert attribute forwarding
    assert delegating.state_schema == "MockStateSchema"
    assert delegating.tools == ["MockTool1", "MockTool2"]
    assert delegating.transformers == ("MockTransformer1",)
    assert delegating.name == "MockInnerName"

    # Assert fallback logic when inner is None
    delegating_none = BaseDelegatingMiddleware(None)
    assert delegating_none.state_schema == getattr(AgentMiddleware, "state_schema")
    assert delegating_none.tools == []
    assert delegating_none.transformers == ()
    assert delegating_none.name == "BaseDelegatingMiddleware"


@pytest.mark.unit
def test_plan_lock_middleware_todos_title_content_fallback():
    from k8s_autopilot.core.middleware.plan_lock import PlanLockMiddleware
    from langchain.agents.middleware import AgentState
    from typing import cast
    
    middleware = PlanLockMiddleware()
    
    # 1. Test when 'title' is present
    state_title = cast(AgentState, {
        "messages": [],
        "todos": [
            {"title": "Step 1", "status": "pending"},
            {"title": "Step 2", "status": "completed"}
        ]
    })
    result = middleware.before_model(state_title, None)
    assert result is not None
    content = result["messages"][0].content
    assert "⏳ Step 1 (pending)" in content
    assert "✅ Step 2 (completed)" in content
    
    # 2. Test when only 'content' is present
    state_content = cast(AgentState, {
        "messages": [],
        "todos": [
            {"content": "Step 1 Content", "status": "pending"},
            {"content": "Step 2 Content", "status": "completed"}
        ]
    })
    result_content = middleware.before_model(state_content, None)
    assert result_content is not None
    content_fallback = result_content["messages"][0].content
    assert "⏳ Step 1 Content (pending)" in content_fallback
    assert "✅ Step 2 Content (completed)" in content_fallback
