"""Unit tests to verify discovery, registration, and instantiation of ported middlewares."""

import pytest
from unittest.mock import MagicMock
from langchain.agents.middleware import AgentMiddleware

from k8s_autopilot.middleware.registry import get_middleware_registry


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
    instance = registry.build_middleware("local_context", working_dir="/tmp")
    assert isinstance(instance, AgentMiddleware)
    assert instance.__class__.__name__ == "LocalContextMiddleware"


@pytest.mark.unit
def test_instantiate_shell_allow_list():
    registry = get_middleware_registry()
    instance = registry.build_middleware("shell_allow_list")
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


# BaseDelegatingMiddleware was part of core/middleware/registry.py
# and has been removed during the core/ elimination migration.
