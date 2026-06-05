import pytest
from unittest.mock import MagicMock
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from deepagents.middleware.summarization import SummarizationToolMiddleware
from k8s_autopilot.core.agents.app_operator.middleware import (
    build_app_operator_middleware,
    AppOperationContextMiddleware,
    PlanLockMiddleware
)

@pytest.fixture
def mock_backend():
    return MagicMock()

@pytest.mark.unit
def test_includes_operation_context_first(mock_config, mock_backend):
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    assert mw[0].__class__.__name__ == "AppOperationContextMiddleware"

@pytest.mark.unit
def test_includes_plan_lock_second(mock_config, mock_backend):
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    assert mw[1].__class__.__name__ == "PlanLockMiddleware"

@pytest.mark.unit
def test_includes_write_file_limit(mock_config, mock_backend):
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    write_limits = [m for m in mw if isinstance(m, ToolCallLimitMiddleware) and m.tool_name == "write_file"]
    assert len(write_limits) > 0

@pytest.mark.unit
def test_includes_global_tool_limit(mock_config, mock_backend):
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    global_limits = [m for m in mw if isinstance(m, ToolCallLimitMiddleware) and getattr(m, "tool_name", None) is None]
    assert len(global_limits) > 0

@pytest.mark.unit
def test_includes_model_call_limit(mock_config, mock_backend):
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    model_limits = [m for m in mw if isinstance(m, ModelCallLimitMiddleware)]
    assert len(model_limits) > 0

@pytest.mark.unit
def test_respects_custom_write_file_limit(mock_config, mock_backend, monkeypatch):
    monkeypatch.setenv("APP_OP_WRITE_FILE_RUN_LIMIT", "5")
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    write_limits = [m for m in mw if isinstance(m, ToolCallLimitMiddleware) and m.tool_name == "write_file"]
    assert write_limits[0].run_limit == 5

@pytest.mark.unit
def test_respects_custom_model_call_limit(mock_config, mock_backend, monkeypatch):
    monkeypatch.setenv("APP_OP_MODEL_CALL_RUN_LIMIT", "15")
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    model_limits = [m for m in mw if isinstance(m, ModelCallLimitMiddleware)]
    assert model_limits[0].run_limit == 15

@pytest.mark.unit
def test_excludes_retry_by_default(mock_config, mock_backend, monkeypatch):
    monkeypatch.setenv("APP_OP_ENABLE_TOOL_RETRY", "false")
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    retry_mws = [m for m in mw if isinstance(m, ToolRetryMiddleware)]
    assert len(retry_mws) == 0

@pytest.mark.unit
def test_includes_retry_when_enabled(mock_config, mock_backend, monkeypatch):
    monkeypatch.setenv("APP_OP_ENABLE_TOOL_RETRY", "true")
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend)
    retry_mws = [m for m in mw if isinstance(m, ToolRetryMiddleware)]
    assert len(retry_mws) > 0

@pytest.mark.unit
def test_includes_summarization_when_model_and_backend(mock_config, mock_backend):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    fake_model = FakeMessagesListChatModel(responses=[])
    mw = build_app_operator_middleware(config=mock_config, model=fake_model, backend=mock_backend)
    summarization = [m for m in mw if isinstance(m, SummarizationToolMiddleware)]
    assert len(summarization) > 0

@pytest.mark.unit
def test_excludes_summarization_when_no_backend(mock_config):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    fake_model = FakeMessagesListChatModel(responses=[])
    mw = build_app_operator_middleware(config=mock_config, model=fake_model, backend=None)
    summarization = [m for m in mw if isinstance(m, SummarizationToolMiddleware)]
    assert len(summarization) == 0

@pytest.mark.unit
def test_extra_middleware_appended(mock_config, mock_backend):
    custom_mw = MagicMock()
    mw = build_app_operator_middleware(config=mock_config, model=MagicMock(), backend=mock_backend, extra_middleware=[custom_mw])
    assert mw[-1] == custom_mw
