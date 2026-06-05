"""
Unit: input_transform — message preservation and file seeding.
"""
import pytest
from langchain_core.messages import HumanMessage

@pytest.mark.unit
def test_input_transform_preserves_messages(coordinator):
    msgs = [HumanMessage(content="Show me Prometheus alerts")]
    result = coordinator.input_transform({"messages": msgs})
    assert result["messages"] == msgs

@pytest.mark.unit
def test_input_transform_calls_seed_files(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "seed_files", lambda *args, **kwargs: {"/skills/observability/prometheus": "content"})
    result = coordinator.input_transform({"messages": []})
    assert "files" in result
    assert result["files"] == {"/skills/observability/prometheus": "content"}

@pytest.mark.unit
def test_input_transform_skips_files_key_when_empty(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "seed_files", lambda *args, **kwargs: {})
    result = coordinator.input_transform({"messages": []})
    assert "files" not in result

@pytest.mark.unit
def test_input_transform_handles_no_messages(coordinator):
    result = coordinator.input_transform({})
    assert result["messages"] == []
