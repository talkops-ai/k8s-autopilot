"""
Eval: App Operator YAML scenario dataset tests.

Runs each scenario from ``tests/evals/dataset/app_operator/*.yaml`` through
the AppOperatorCoordinator with a scripted fake model, then evaluates the
trajectory using all 6 rule-based evaluators.

Design decisions (LangChain agentic testing best practices):
- Uses a fake model in CI: fast, no API key, deterministic routing check
- Each scenario maps to one test function for independent pass/fail reporting
- The evaluator pipeline covers ALL 6 expectation fields in the YAML schema:
  must_call_subagents, forbidden_subagents, must_call_tools, forbidden_tools,
  safety_requirements.must_trigger_hitl, safety_requirements.must_log_operation
- Scenarios with ``skip_agent_run: true`` are skipped gracefully

To run with a real LLM (for trajectory quality evaluation):
    EVAL_USE_REAL_LLM=1 pytest tests/evals/test_app_operator_eval_scenarios.py -v
"""
import os
import pytest
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from tests.evals.app_operator.runner import load_app_operator_dataset, run_app_operator_scenario
from tests.evals.evaluators.rule_based import (
    evaluate_required_subagents,
    evaluate_forbidden_subagents,
    evaluate_required_tools,
    evaluate_forbidden_tools,
    evaluate_hitl_gate,
    evaluate_operation_logging,
    evaluate_oos_response,
)


# ---------------------------------------------------------------------------
# Fake model factory — returns a model that gives a neutral "outside scope" or
# delegating response based on the scenario's expected routing.
# ---------------------------------------------------------------------------

def _make_scenario_fake_model(scenario: dict) -> FakeMessagesListChatModel:
    """
    Build a scripted fake model for CI smoke-testing.

    If the scenario expects no subagents (OOS), return an OOS response.
    Otherwise return a minimal delegating response so evaluators can check
    that routing logic in the scenario file is internally consistent.

    Note: This tests the EVALUATOR pipeline, not the real agent routing.
    Real routing is tested by integration tests and live eval runs.
    """
    expectations = scenario.get("expectations", {})
    must_call = expectations.get("must_call_subagents", [])
    final_outcome = expectations.get("final_outcome", "")

    if not must_call or "oos" in final_outcome or "out_of_scope" in final_outcome:
        return FakeMessagesListChatModel(responses=[
            AIMessage(content="This is outside my scope. Please use the appropriate operator. "
                              f"User Request: {scenario.get('user_request', '')} "
                              "Context: No prior context.")
        ])

    # Build a response that delegates to the first required subagent
    primary_subagent = must_call[0] if must_call else "argocd-onboarder"
    return FakeMessagesListChatModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "id": "tc1",
                "name": primary_subagent,
                "args": {"task": f"[STATE-MODIFYING] {scenario.get('user_request', '')}"},
            }],
        ),
        AIMessage(content=f"Completed {primary_subagent} operation successfully."),
    ])


# ---------------------------------------------------------------------------
# Parametrized test — one test per scenario file
# ---------------------------------------------------------------------------

_SCENARIOS = load_app_operator_dataset()
_SCENARIO_IDS = [s.get("id", f"scenario_{i}") for i, s in enumerate(_SCENARIOS)]


@pytest.mark.eval
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=_SCENARIO_IDS)
@pytest.mark.timeout(120)
async def test_app_operator_eval_scenario(scenario):
    """
    Run a YAML eval scenario through the full evaluator pipeline.

    Each scenario runs with a scripted fake model in CI to validate the
    evaluator pipeline itself. When EVAL_USE_REAL_LLM=1 is set, the real
    coordinator model is used instead.
    """
    use_real_llm = os.environ.get("EVAL_USE_REAL_LLM", "").lower() in ("1", "true", "yes")

    if scenario.get("skip_agent_run") and not use_real_llm:
        pytest.skip(f"Scenario '{scenario['id']}' has skip_agent_run=true")

    fake_model = None if use_real_llm else _make_scenario_fake_model(scenario)
    trace = await run_app_operator_scenario(scenario, fake_model=fake_model)

    if trace.get("skipped"):
        pytest.skip(trace.get("skip_reason", "skipped"))

    # Run all 6 evaluators — collect failures for a comprehensive report
    failures = []

    results = {
        "required_subagents": evaluate_required_subagents(trace, scenario),
        "forbidden_subagents": evaluate_forbidden_subagents(trace, scenario),
        "required_tools":     evaluate_required_tools(trace, scenario),
        "forbidden_tools":    evaluate_forbidden_tools(trace, scenario),
        "hitl_gate":          evaluate_hitl_gate(trace, scenario),
        "operation_logging":  evaluate_operation_logging(trace, scenario),
        "oos_response":       evaluate_oos_response(trace, scenario),
    }

    for name, result in results.items():
        if not result.passed:
            failures.append(f"  [{name}] FAILED — {result.rationale}")

    if failures:
        failure_report = "\n".join(failures)
        chat_paused = trace.get("chat_paused", False)
        hitl_triggered = trace.get("hitl_triggered", False)
        tool_names = [tc["name"] for tc in trace.get("tool_calls", [])]

        pytest.fail(
            f"Scenario '{scenario['id']}' failed evaluators:\n{failure_report}\n\n"
            f"--- Trace Diagnostics ---\n"
            f"  subagents_called: {trace.get('subagents_called')}\n"
            f"  tool_calls:       {tool_names}\n"
            f"  chat_paused:      {chat_paused}  "
            f"{'← agent asked for more info (context injection may be needed)' if chat_paused else ''}\n"
            f"  hitl_triggered:   {hitl_triggered}  "
            f"{'← HITL interrupt fired (captured partial trace)' if hitl_triggered else ''}\n"
            f"  final_message:    {trace.get('final_message', '')[:300]}"
        )

