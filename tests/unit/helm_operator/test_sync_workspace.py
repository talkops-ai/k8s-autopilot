import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


@pytest.fixture
def coordinator(mock_config):
    return HelmOperatorCoordinator(config=mock_config)


@patch("k8s_autopilot.core.agents.helm_operator.coordinator.sync_workspace_to_disk")
@pytest.mark.asyncio
async def test_sync_workspace_returns_no_files_message_when_empty(mock_sync, coordinator):
    tools = await coordinator.get_tools()
    sync_tool = next(t for t in tools if t.name == "sync_workspace")
    class DummyRuntime:
        state = {"files": {}}
    result = sync_tool.func(runtime=DummyRuntime())
    assert "No /workspace/ files found" in result


@patch("k8s_autopilot.core.agents.helm_operator.coordinator.sync_workspace_to_disk")
@pytest.mark.asyncio
async def test_sync_workspace_syncs_workspace_prefix_files(mock_sync, coordinator):
    mock_sync.return_value = {"/workspace/charts/nginx/Chart.yaml": "/tmp/Chart.yaml"}
    tools = await coordinator.get_tools()
    sync_tool = next(t for t in tools if t.name == "sync_workspace")
    class DummyRuntime:
        state = {"files": {"/workspace/charts/nginx/Chart.yaml": {"content": "apiVersion: v2"}}}
    sync_tool.func(runtime=DummyRuntime())
    mock_sync.assert_called_once()


@patch("k8s_autopilot.core.agents.helm_operator.coordinator.sync_workspace_to_disk")
@pytest.mark.asyncio
async def test_sync_workspace_returns_synced_count(mock_sync, coordinator):
    mock_sync.return_value = {
        "/workspace/charts/nginx/Chart.yaml": "/tmp/Chart.yaml",
        "/workspace/charts/nginx/values.yaml": "/tmp/values.yaml",
        "/workspace/charts/nginx/templates/deployment.yaml": "/tmp/deployment.yaml",
    }
    tools = await coordinator.get_tools()
    sync_tool = next(t for t in tools if t.name == "sync_workspace")
    class DummyRuntime:
        state = {"files": {"/workspace/charts/nginx/Chart.yaml": {}, "/workspace/charts/nginx/values.yaml": {}, "/workspace/charts/nginx/templates/deployment.yaml": {}}}
    result = sync_tool.func(runtime=DummyRuntime())
    assert "Synced 3 file(s)" in result


@patch("k8s_autopilot.core.agents.helm_operator.coordinator.sync_workspace_to_disk")
@pytest.mark.asyncio
async def test_sync_workspace_no_workspace_prefix_files(mock_sync, coordinator):
    mock_sync.return_value = {}
    tools = await coordinator.get_tools()
    sync_tool = next(t for t in tools if t.name == "sync_workspace")
    class DummyRuntime:
        state = {"files": {"/skills/foo.md": {"content": "foo"}}}
    result = sync_tool.func(runtime=DummyRuntime())
    assert "No /workspace/ files found" in result
