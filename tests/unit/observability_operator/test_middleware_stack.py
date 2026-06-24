"""
Unit: build_obs_operator_middleware — stack ordering and env var overrides.

Bug classes caught:
- Order violation → ops context injected AFTER summarization prunes it
- No write/tool/model limits → runaway agent burns API budget
- Custom limits ignored → default values when user expects custom
- Retry enabled for HITL tools → retries GraphInterrupt
"""
import pytest
from unittest.mock import MagicMock
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from deepagents.middleware.summarization import SummarizationToolMiddleware
from k8s_autopilot.core.agents.observability.middleware import (
    build_obs_operator_middleware,
    ObsOperationContextMiddleware,
)
from k8s_autopilot.core.agents.app_operator.middleware import PlanLockMiddleware

@pytest.fixture
def mock_backend():
    return MagicMock()

@pytest.mark.unit
def test_includes_operation_context_first(mock_config, mock_backend):
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    assert mw[0].__class__.__name__ == "ObsOperationContextMiddleware"

@pytest.mark.unit
def test_includes_plan_lock_second(mock_config, mock_backend):
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    assert mw[1].__class__.__name__ == "PlanLockMiddleware"

@pytest.mark.unit
def test_includes_write_file_limit(mock_config, mock_backend):
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    write_limits = [m for m in mw if isinstance(m, ToolCallLimitMiddleware) and m.tool_name == "write_file"]
    assert len(write_limits) > 0

@pytest.mark.unit
def test_includes_global_tool_limit(mock_config, mock_backend):
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    global_limits = [m for m in mw if isinstance(m, ToolCallLimitMiddleware) and getattr(m, "tool_name", None) is None]
    assert len(global_limits) > 0

@pytest.mark.unit
def test_includes_model_call_limit(mock_config, mock_backend):
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    model_limits = [m for m in mw if isinstance(m, ModelCallLimitMiddleware)]
    assert len(model_limits) > 0

@pytest.mark.unit
def test_respects_custom_write_file_limit_kwarg(mock_config, mock_backend):
    """build_obs_operator_middleware accepts write_file_limit as kwarg.
    NOTE: There is no env var override for this — see production finding."""
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend, write_file_limit=5)
    write_limits = [m for m in mw if isinstance(m, ToolCallLimitMiddleware) and m.tool_name == "write_file"]
    assert write_limits[0].run_limit == 5

@pytest.mark.unit
def test_respects_custom_model_call_limit_kwarg(mock_config, mock_backend):
    """build_obs_operator_middleware accepts model_call_limit as kwarg.
    NOTE: There is no env var override for this — see production finding."""
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend, model_call_limit=15)
    model_limits = [m for m in mw if isinstance(m, ModelCallLimitMiddleware)]
    assert model_limits[0].run_limit == 15

@pytest.mark.unit
def test_excludes_retry_by_default(mock_config, mock_backend):
    """Default: no retry middleware."""
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    retry_mws = [m for m in mw if isinstance(m, ToolRetryMiddleware)]
    assert len(retry_mws) == 0

@pytest.mark.unit
def test_includes_retry_when_enabled(mock_config, mock_backend):
    """When enable_tool_retry=True kwarg is passed, retry middleware is added."""
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend, enable_tool_retry=True)
    retry_mws = [m for m in mw if isinstance(m, ToolRetryMiddleware)]
    assert len(retry_mws) > 0

@pytest.mark.unit
def test_includes_summarization_when_model_and_backend(mock_config, mock_backend):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    fake_model = FakeMessagesListChatModel(responses=[])
    mw = build_obs_operator_middleware(config=mock_config, model=fake_model, backend=mock_backend)
    summarization = [m for m in mw if isinstance(m, SummarizationToolMiddleware)]
    assert len(summarization) > 0

@pytest.mark.unit
def test_excludes_summarization_when_no_backend(mock_config):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    fake_model = FakeMessagesListChatModel(responses=[])
    mw = build_obs_operator_middleware(config=mock_config, model=fake_model, backend=None)
    summarization = [m for m in mw if isinstance(m, SummarizationToolMiddleware)]
    assert len(summarization) == 0

@pytest.mark.unit
def test_extra_middleware_appended(mock_config, mock_backend):
    custom_mw = MagicMock()
    mw = build_obs_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend, extra_middleware=[custom_mw])
    assert mw[-1] == custom_mw
