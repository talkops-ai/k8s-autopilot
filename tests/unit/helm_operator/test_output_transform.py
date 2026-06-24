import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


@pytest.fixture
def coordinator(mock_config):
    return HelmOperatorCoordinator(config=mock_config)


def test_output_transform_extracts_final_message(coordinator):
    state = {"messages": [AIMessage(content="Final result")]}
    result = coordinator.output_transform(state)
    assert result["final_message"] == "Final result"


def test_output_transform_defaults_final_message(coordinator):
    state = {"messages": []}
    result = coordinator.output_transform(state)
    assert result["final_message"] == "Helm operator completed."


def test_output_transform_includes_status_completed(coordinator):
    state = {"messages": []}
    result = coordinator.output_transform(state)
    assert result["status"] == "completed"


@patch("k8s_autopilot.core.agents.helm_operator.coordinator.sync_workspace_to_disk")
def test_output_transform_syncs_workspace_files(mock_sync, coordinator):
    mock_sync.return_value = {"/workspace/charts/nginx/Chart.yaml": "/tmp/Chart.yaml"}
    state = {"files": {"/workspace/charts/nginx/Chart.yaml": "content"}}
    result = coordinator.output_transform(state)
    mock_sync.assert_called_once_with(state["files"])
    assert "synced_paths" in result["helm_operator_output"]


@patch("k8s_autopilot.core.agents.helm_operator.coordinator.sync_workspace_to_disk")
def test_output_transform_skips_sync_for_non_workspace_files(mock_sync, coordinator):
    state = {"files": {"/skills/foo.md": "content"}}
    coordinator.output_transform(state)
    mock_sync.assert_not_called()


def test_output_transform_builds_domain_summary(coordinator):
    state = {"messages": [AIMessage(content="Final result")]}
    result = coordinator.output_transform(state)
    assert result.get("domain_summary") is not None


def test_output_transform_handles_pydantic_model_input(coordinator):
    class MockState:
        def model_dump(self):
            return {"messages": [AIMessage(content="Pydantic result")]}
    
    result = coordinator.output_transform(MockState())
    assert result["final_message"] == "Pydantic result"
