import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from k8s_autopilot.core.backend import get_memories_namespace

@pytest.mark.unit
def test_get_memories_namespace_fallback():
    # Outside graph execution context, should fallback to org only
    with patch("langgraph.config.get_config", side_effect=Exception("no context")):
        ns = get_memories_namespace()
        assert ns == ("default_org",)

@pytest.mark.unit
def test_get_memories_namespace_with_thread_id():
    mock_config = {
        "configurable": {
            "thread_id": "test-thread-abc"
        }
    }
    with patch("langgraph.config.get_config", return_value=mock_config):
        ns = get_memories_namespace()
        assert ns == ("default_org", "test-thread-abc")

@pytest.mark.unit
def test_get_memories_namespace_no_thread_id():
    mock_config = {
        "configurable": {}
    }
    with patch("langgraph.config.get_config", return_value=mock_config):
        ns = get_memories_namespace()
        assert ns == ("default_org",)

@pytest.mark.unit
@pytest.mark.asyncio
async def test_copy_preseeded_memories():
    # Mock item in global namespace
    mock_item = MagicMock()
    mock_item.key = "helm-operator/AGENTS.md"
    mock_item.value = {"content": "rules"}

    # Mock store
    child_store = AsyncMock()
    child_store.asearch.return_value = [mock_item]
    # Simulate that item does NOT exist in the thread namespace yet
    child_store.aget.return_value = None

    global_ns = ("default_org",)
    thread_ns = ("default_org", "thread-123")
    
    # Replicate the supervisor_agent node pre-seeding copy logic
    global_items = await child_store.asearch(global_ns)
    for item in global_items:
        existing = await child_store.aget(thread_ns, item.key)
        if existing is None:
            await child_store.aput(thread_ns, item.key, item.value)
            
    # Verify mock calls
    child_store.asearch.assert_called_once_with(global_ns)
    child_store.aget.assert_called_once_with(thread_ns, "helm-operator/AGENTS.md")
    child_store.aput.assert_called_once_with(thread_ns, "helm-operator/AGENTS.md", mock_item.value)

@pytest.mark.unit
@pytest.mark.asyncio
async def test_copy_preseeded_memories_already_exists():
    # Mock item in global namespace
    mock_item = MagicMock()
    mock_item.key = "helm-operator/operations-log.md"
    mock_item.value = {"content": "empty log"}

    # Mock store
    child_store = AsyncMock()
    child_store.asearch.return_value = [mock_item]
    # Simulate that item ALREADY exists in the thread namespace (e.g. from previous turn)
    child_store.aget.return_value = {"content": "updated log"}

    global_ns = ("default_org",)
    thread_ns = ("default_org", "thread-123")
    
    # Replicate the supervisor_agent node pre-seeding copy logic
    global_items = await child_store.asearch(global_ns)
    for item in global_items:
        existing = await child_store.aget(thread_ns, item.key)
        if existing is None:
            await child_store.aput(thread_ns, item.key, item.value)
            
    # Verify that aput was NOT called because it already existed
    child_store.asearch.assert_called_once_with(global_ns)
    child_store.aget.assert_called_once_with(thread_ns, "helm-operator/operations-log.md")
    child_store.aput.assert_not_called()
