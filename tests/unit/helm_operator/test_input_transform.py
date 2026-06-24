import pytest
from langchain_core.messages import HumanMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


@pytest.fixture
def coordinator(mock_config):
    return HelmOperatorCoordinator(config=mock_config)


def test_input_transform_preserves_messages(coordinator):
    msg = HumanMessage(content="test")
    payload = {"messages": [msg]}
    result = coordinator.input_transform(payload)
    assert result["messages"] == [msg]


def test_input_transform_calls_seed_files(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "seed_files", lambda **kwargs: {"/skills/test.md": "content"})
    result = coordinator.input_transform({})
    assert "/skills/test.md" in result["files"]


def test_input_transform_skips_files_key_when_empty(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "seed_files", lambda **kwargs: {})
    result = coordinator.input_transform({})
    assert "files" not in result


def test_input_transform_handles_no_messages(coordinator):
    result = coordinator.input_transform({})
    assert result["messages"] == []
