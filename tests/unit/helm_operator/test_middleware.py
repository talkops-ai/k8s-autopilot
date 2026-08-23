import pytest
from langchain_core.messages import SystemMessage
from langchain.agents.middleware import AgentState
from k8s_autopilot.core.middleware.operation_context import OperationContextMiddleware
from typing import cast

from unittest.mock import patch, MagicMock

@pytest.fixture(autouse=True)
def mock_genai_model():
    mock_result = MagicMock()
    mock_result.model_name = "fake-model"
    mock_result.provider = "google_genai"
    mock_result.context_limit = 1000000
    mock_result.unsupported_modalities = frozenset()
    mock_result.model = MagicMock()
    
    with patch("k8s_autopilot.core.agents.helm_operator.coordinator.create_model_with_result", return_value=mock_result):
        yield mock_result


def test_operation_context_middleware_injects_system_message():
    middleware = OperationContextMiddleware(
        log_path="/memories/helm-operator/operations-log.md",
        prefix="Helm",
    )
    state = {
        "files": {
            "/memories/helm-operator/operations-log.md": {
                "content": "Action: install\nRelease: nginx"
            }
        }
    }
    result = middleware.before_model(cast(AgentState, state), runtime=None)
    assert result is not None
    assert "messages" in result
    assert isinstance(result["messages"][0], SystemMessage)
    assert "Action: install" in result["messages"][0].content


def test_operation_context_middleware_returns_none_when_empty_log():
    middleware = OperationContextMiddleware(
        log_path="/memories/helm-operator/operations-log.md",
        prefix="Helm",
    )
    state = {"files": {}}
    result = middleware.before_model(cast(AgentState, state), runtime=None)
    assert result is None

    state2 = {
        "files": {
            "/memories/helm-operator/operations-log.md": {
                "content": "# Helm Operations Journal\n\nAuto-generated log of operations performed in this session. Used by the coordinator to maintain context across conversation turns and after summarization.\n"
            }
        }
    }
    result2 = middleware.before_model(cast(AgentState, state2), runtime=None)
    assert result2 is None


@pytest.mark.asyncio
async def test_operation_context_middleware_async_delegates_to_sync():
    middleware = OperationContextMiddleware(
        log_path="/memories/helm-operator/operations-log.md",
        prefix="Helm",
    )
    state = {
        "files": {
            "/memories/helm-operator/operations-log.md": {
                "content": "Action: install\nRelease: nginx"
            }
        }
    }
    result_async = await middleware.abefore_model(cast(AgentState, state), runtime=None)
    result_sync = middleware.before_model(cast(AgentState, state), runtime=None)
    assert result_async is not None
    assert result_sync is not None
    assert result_async["messages"][0].content == result_sync["messages"][0].content


from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


def test_build_coordinator_middleware_excludes_operation_context(mock_config):
    coordinator = HelmOperatorCoordinator(config=mock_config)
    middleware_stack = coordinator._build_middleware()
    names = [m.__class__.__name__ for m in middleware_stack]
    assert "OperationContextMiddleware" not in names


def test_build_coordinator_middleware_respects_write_file_limit(mock_config):
    coordinator = HelmOperatorCoordinator(config=mock_config)
    middleware_stack = coordinator._build_middleware()
    tool_limit_middlewares = [m for m in middleware_stack if m.__class__.__name__ == "ToolCallLimitMiddleware"]
    assert any(m.run_limit == 20 for m in tool_limit_middlewares) # Default write limit


def test_build_coordinator_middleware_respects_model_call_limit(mock_config):
    coordinator = HelmOperatorCoordinator(config=mock_config)
    middleware_stack = coordinator._build_middleware()
    model_limit_middlewares = [m for m in middleware_stack if m.__class__.__name__ == "ModelCallLimitMiddleware"]
    assert any(m.run_limit == 40 for m in model_limit_middlewares) # Default model limit


class TestApprovalDescription:
    def test_approval_description_install_includes_chart_and_release(self):
        from k8s_autopilot.core.agents.helm_operator.middleware import _build_approval_description
        desc = _build_approval_description("helm_install_chart", {"chart_name": "nginx", "release_name": "web", "namespace": "prod"})
        assert "nginx" in desc
        assert "web" in desc
        assert "prod" in desc
        assert "INSTALLATION" in desc

    def test_approval_description_upgrade_includes_release(self):
        from k8s_autopilot.core.agents.helm_operator.middleware import _build_approval_description
        desc = _build_approval_description("helm_upgrade_release", {"release_name": "web", "chart_name": "nginx", "namespace": "prod"})
        assert "UPGRADE" in desc
        assert "web" in desc

    def test_approval_description_rollback_includes_revision(self):
        from k8s_autopilot.core.agents.helm_operator.middleware import _build_approval_description
        desc = _build_approval_description("helm_rollback_release", {"release_name": "web", "revision": 3})
        assert "ROLLBACK" in desc
        assert "3" in desc

    def test_approval_description_uninstall_includes_warning(self):
        from k8s_autopilot.core.agents.helm_operator.middleware import _build_approval_description
        desc = _build_approval_description("helm_uninstall_release", {"release_name": "web"})
        assert "UNINSTALL" in desc
        assert "⚠️" in desc

    def _get_helm_operation_spec(self):
        from k8s_autopilot.core.agents.registry import list_subagents, get_domain_agents_dir
        from k8s_autopilot.core.agents.helm_operator.middleware import build_dynamic_subagent_spec

        agents_dir = get_domain_agents_dir("helm-operator")
        metas = list_subagents(agents_dirs=[agents_dir])
        meta = next((m for m in metas if m.name == "helm-operation"), None)
        assert meta is not None, "Could not find helm-operation subagent"
        return build_dynamic_subagent_spec(meta)

    def test_dynamic_hitl_gates_install(self):
        spec = self._get_helm_operation_spec()
        assert "interrupt_on" in spec
        assert "helm_install_chart" in spec["interrupt_on"]

    def test_dynamic_hitl_allows_approve_edit_reject_for_install(self):
        spec = self._get_helm_operation_spec()
        val = spec["interrupt_on"]["helm_install_chart"]
        if hasattr(val, "allowed_decisions"):
            assert "approve" in val.allowed_decisions
            assert "edit" in val.allowed_decisions
            assert "reject" in val.allowed_decisions
        elif isinstance(val, dict) and "allowed_decisions" in val:
            assert "approve" in val["allowed_decisions"]

    def test_dynamic_hitl_only_approve_reject_for_rollback(self):
        spec = self._get_helm_operation_spec()
        val = spec["interrupt_on"]["helm_rollback_release"]
        if hasattr(val, "allowed_decisions"):
            assert "approve" in val.allowed_decisions
            assert "reject" in val.allowed_decisions
            assert "edit" not in val.allowed_decisions
        elif isinstance(val, dict) and "allowed_decisions" in val:
            assert "approve" in val["allowed_decisions"]
