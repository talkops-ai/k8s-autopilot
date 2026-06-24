import pytest
from unittest.mock import MagicMock
from langchain.agents.middleware import ModelCallLimitMiddleware

@pytest.fixture
def model_limit_middleware():
    """CRITICAL: Prevents FakeModel infinite loops."""
    return ModelCallLimitMiddleware(run_limit=10, exit_behavior="end")
