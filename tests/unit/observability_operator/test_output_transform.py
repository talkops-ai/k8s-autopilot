"""
Unit: output_transform — message extraction, fallback, domain summary.

Bug classes caught:
- final_message extraction failure → supervisor gets None
- Missing status → supervisor can't determine if agent finished
- Domain summary missing → cross-domain blackboard pattern broken
- Pydantic vs dict → AttributeError in production
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

@pytest.mark.unit
def test_output_transform_extracts_final_message(coordinator):
    state = {"messages": [AIMessage(content="Prometheus query result: CPU at 45%")]}
    result = coordinator.output_transform(state)
    assert result["summary_text"] == "Prometheus query result: CPU at 45%"

@pytest.mark.unit
def test_output_transform_defaults_final_message(coordinator):
    result = coordinator.output_transform({"messages": []})
    assert result["summary_text"] == "Observability operator completed."

@pytest.mark.unit
def test_output_transform_includes_status_completed(coordinator):
    result = coordinator.output_transform({"messages": []})
    assert result["status"] == "completed"

@pytest.mark.unit
def test_output_transform_includes_observability_output(coordinator):
    state = {
        "messages": [AIMessage(content="msg")],
        "structured_response": {"alerts": ["HighCPU"]},
    }
    result = coordinator.output_transform(state)
    assert "messages" in result["artifacts"]
    assert "structured_response" in result["artifacts"]
    assert result["artifacts"]["structured_response"] == {"alerts": ["HighCPU"]}

@pytest.mark.unit
def test_output_transform_builds_domain_summary(coordinator, monkeypatch):
    monkeypatch.setattr(
        "k8s_autopilot.core.agents.observability.coordinator.extract_domain_summary",
        lambda domain, final_message: {"domain": domain, "summary": final_message},
    )
    state = {"messages": [AIMessage(content="Alert silenced")]}
    result = coordinator.output_transform(state)
    assert result["domain_summary"] is not None
    assert result["domain_summary"]["domain"] == "observability"
    assert "Alert silenced" in result["domain_summary"]["summary"]

@pytest.mark.unit
def test_output_transform_handles_pydantic_model_input(coordinator):
    class DummyModel:
        def model_dump(self):
            return {"messages": [AIMessage(content="pydantic final")]}

    result = coordinator.output_transform(DummyModel())
    assert result["summary_text"] == "pydantic final"

@pytest.mark.unit
def test_output_transform_handles_dict_message(coordinator):
    state = {"messages": [{"type": "ai", "content": "dict content"}]}
    result = coordinator.output_transform(state)
    assert result["summary_text"] == "dict content"
