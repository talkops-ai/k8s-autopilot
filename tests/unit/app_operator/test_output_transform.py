import pytest
from langchain_core.messages import AIMessage, HumanMessage

@pytest.mark.unit
def test_output_transform_extracts_final_message(coordinator):
    state = {"messages": [AIMessage(content="Final response")]}
    result = coordinator.output_transform(state)
    assert result["final_message"] == "Final response"

@pytest.mark.unit
def test_output_transform_defaults_final_message(coordinator):
    result = coordinator.output_transform({"messages": []})
    assert result["final_message"] == "App operator completed."

@pytest.mark.unit
def test_output_transform_includes_status_completed(coordinator):
    result = coordinator.output_transform({"messages": []})
    assert result["status"] == "completed"

@pytest.mark.unit
def test_output_transform_includes_app_operator_output(coordinator):
    state = {
        "messages": [AIMessage(content="msg")],
        "structured_response": {"key": "value"}
    }
    result = coordinator.output_transform(state)
    assert "messages" in result["app_operator_output"]
    assert "structured_response" in result["app_operator_output"]
    assert result["app_operator_output"]["structured_response"] == {"key": "value"}

@pytest.mark.unit
def test_output_transform_builds_domain_summary(coordinator, monkeypatch):
    monkeypatch.setattr(
        "k8s_autopilot.core.agents.app_operator.coordinator.extract_domain_summary",
        lambda domain, final_message: {"domain": domain, "summary": final_message}
    )
    state = {"messages": [AIMessage(content="Something happened")]}
    result = coordinator.output_transform(state)
    assert result["domain_summary"] is not None
    assert result["domain_summary"]["domain"] == "app"
    assert "Something happened" in result["domain_summary"]["summary"]

@pytest.mark.unit
def test_output_transform_handles_pydantic_model_input(coordinator):
    class DummyModel:
        def model_dump(self):
            return {"messages": [AIMessage(content="pydantic final")]}
    
    result = coordinator.output_transform(DummyModel())
    assert result["final_message"] == "pydantic final"

@pytest.mark.unit
def test_output_transform_handles_dict_message(coordinator):
    state = {"messages": [{"role": "assistant", "content": "dict content"}]}
    result = coordinator.output_transform(state)
    assert result["final_message"] == "dict content"
