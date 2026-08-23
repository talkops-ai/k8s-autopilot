import pytest
from unittest.mock import patch, MagicMock
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from deepagents.middleware.subagents import CompiledSubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.messages import AIMessage


class MockChatModel(BaseChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="mock"))])
    @property
    def _llm_type(self) -> str:
        return "mock"


@pytest.fixture(autouse=True)
def mock_genai_model():
    mock_result = MagicMock()
    mock_result.model_name = "fake-model"
    mock_result.provider = "google_genai"
    mock_result.context_limit = 1000000
    mock_result.unsupported_modalities = frozenset()
    mock_result.model = MockChatModel()
    
    with patch("k8s_autopilot.core.agents.helm_operator.coordinator.create_model_with_result", return_value=mock_result):
        yield mock_result


@pytest.fixture
def coordinator(mock_config):
    return HelmOperatorCoordinator(config=mock_config)


@pytest.mark.asyncio
async def test_recursive_nested_agent_compilation(coordinator):
    """Verify that get_subagent_specs recursively compiles deep subagents as CompiledSubAgent."""
    # Discover and build specs
    specs = await coordinator.get_subagent_specs()

    # The specs list should contain both helm-coder and helm-operation
    assert len(specs) == 2
    
    # Verify helm-coder is a CompiledSubAgent
    helm_coder_spec = next((s for s in specs if s["name"] == "helm-coder"), None)
    assert helm_coder_spec is not None
    assert "runnable" in helm_coder_spec
    assert helm_coder_spec["name"] == "helm-coder"
    assert "deep coordinator agent for Helm coding tasks" in helm_coder_spec["description"]

    # Verify helm-operation is a standard react-type subagent spec (dict representation)
    helm_op_spec = next((s for s in specs if s["name"] == "helm-operation"), None)
    assert helm_op_spec is not None
    assert "runnable" not in helm_op_spec
    assert helm_op_spec["system_prompt"] is not None
