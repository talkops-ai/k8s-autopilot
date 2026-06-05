"""
Helm Operator Trajectory Evals.

These tests run full agent scenarios through the HelmOperatorCoordinator and
evaluate the resulting trace against rule-based and LLM-as-judge evaluators.

Marked @pytest.mark.slow — skipped by default in CI fast-path.

Run with:
    # Full real LLM eval:
    EVAL_USE_REAL_LLM=1 pytest tests/evals/test_helm_operator_evals.py -v

    # Smoke-test (fake model, no API key needed):
    pytest tests/evals/test_helm_operator_evals.py -m "eval and not slow"

Design notes:
- Subagents are always MOCKED so the eval tests coordinator routing decisions,
  not whether Helm/GitHub MCP servers are reachable.
- Scenario context is injected as SystemMessage so no mid-trace clarification
  requests interrupt the trace.
- All 7 rule-based evaluators are run per scenario and failures are COLLECTED
  (not asserted on first fail) — this means a single run shows ALL failures.
- LLM-as-judge is run only when EVAL_USE_REAL_LLM=1 to keep CI fast.
"""

import os
import pytest
from tests.evals.helm_operator.runner import load_helm_dataset, run_helm_operator_scenario
from tests.evals.evaluators.rule_based import (
    evaluate_required_subagents,
    evaluate_forbidden_subagents,
    evaluate_required_tools,
    evaluate_forbidden_tools,
    evaluate_commit_hitl_gate,
    evaluate_hitl_gate,
    evaluate_operation_logging,
    evaluate_oos_response,
)

scenarios = load_helm_dataset()


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", scenarios, ids=lambda x: x["id"])
async def test_helm_operator_evals(scenario):
    """
    End-to-end trajectory eval for each scenario in dataset/.

    Uses mocked subagents + real HelmOperatorCoordinator with Config() sourced
    from environment variables / .env (same as production).

    Skipped automatically when:
    - scenario is marked skip_agent_run=true
    - EVAL_USE_REAL_LLM env var is not set (prevents accidental LLM API calls)
    """
    use_real_llm = os.environ.get("EVAL_USE_REAL_LLM")
    if not use_real_llm:
        pytest.skip(
            "Skipping real LLM eval — set EVAL_USE_REAL_LLM=1 to run. "
            "Use 'pytest -m eval and not slow' for evaluator-only tests."
        )

    if scenario.get("skip_agent_run"):
        pytest.skip(f"Skipping scenario '{scenario['id']}' — skip_agent_run=true")

    trace = await run_helm_operator_scenario(scenario)

    # ── Collect all rule-based failures before asserting ───────────────────
    # This approach shows ALL failing evaluators per scenario, not just the first.
    eval_failures = []

    def _check(result, label: str):
        if not result.passed:
            eval_failures.append(f"[{label}] {result.rationale}")

    _check(evaluate_required_subagents(trace, scenario), "required_subagents")
    _check(evaluate_forbidden_subagents(trace, scenario), "forbidden_subagents")
    _check(evaluate_required_tools(trace, scenario), "required_tools")
    _check(evaluate_forbidden_tools(trace, scenario), "forbidden_tools")
    _check(evaluate_commit_hitl_gate(trace, scenario), "commit_hitl_gate")
    _check(evaluate_hitl_gate(trace, scenario), "hitl_gate")
    _check(evaluate_operation_logging(trace, scenario), "operation_logging")
    _check(evaluate_oos_response(trace, scenario), "oos_response")

    if eval_failures:
        failure_summary = "\n".join(f"  • {f}" for f in eval_failures)
        trace_summary = _format_trace_for_diagnostics(trace)
        pytest.fail(
            f"[{scenario['id']}] {len(eval_failures)} rule-based evaluator(s) failed:\n"
            f"{failure_summary}\n\n"
            f"Trace diagnostics:\n{trace_summary}"
        )

    # ── LLM-as-judge ───────────────────────────────────────────────────────
    from tests.evals.evaluators.llm_judge import evaluate_agent_trajectory
    from k8s_autopilot.utils.llm import create_model
    from k8s_autopilot.config.config import Config

    cfg = Config()
    judge_llm = create_model(cfg.get_llm_deepagent_config())

    res_llm = await evaluate_agent_trajectory(judge_llm, trace, scenario)
    assert res_llm.passed, (
        f"[{scenario['id']}] LLM judge: {res_llm.rationale}\n\n"
        f"Trace diagnostics:\n{_format_trace_for_diagnostics(trace)}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Smoke-test: evaluator pipeline with fake model (no API key, fast)
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.eval
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", scenarios, ids=lambda x: x["id"])
async def test_helm_evaluator_pipeline_smoke(scenario):
    """
    Smoke-test: run each scenario with a FakeModel to verify the evaluator
    pipeline doesn't crash and produces coherent pass/fail.

    Does NOT assert pass — the fake model won't route correctly.
    Asserts: the runner completes, evaluators return EvalResult objects,
    and the trace contains at least one message.
    """
    if scenario.get("skip_agent_run"):
        pytest.skip(f"Skipping scenario '{scenario['id']}' — skip_agent_run=true")

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self

    # Simple fake model — produces OOS string to prevent tool-call loops
    fake = BindableFakeModel(responses=[
        AIMessage(content=(
            "This is outside my scope. Please use the appropriate operator.\n"
            "User Request: test\n"
            "Context: smoke test"
        )),
    ])

    trace = await run_helm_operator_scenario(scenario, fake_model=fake)

    # Trace structure must always be present regardless of model output
    assert "subagents_called" in trace
    assert "tool_calls" in trace
    assert "final_message" in trace
    assert "messages" in trace

    # Evaluators must return valid EvalResult objects without crashing
    results = [
        evaluate_required_subagents(trace, scenario),
        evaluate_forbidden_subagents(trace, scenario),
        evaluate_required_tools(trace, scenario),
        evaluate_forbidden_tools(trace, scenario),
        evaluate_commit_hitl_gate(trace, scenario),
        evaluate_hitl_gate(trace, scenario),
        evaluate_operation_logging(trace, scenario),
        evaluate_oos_response(trace, scenario),
    ]

    for r in results:
        assert hasattr(r, "passed"), f"EvalResult missing 'passed' field: {r}"
        assert hasattr(r, "rationale"), f"EvalResult missing 'rationale' field: {r}"
        assert isinstance(r.passed, bool), f"EvalResult.passed must be bool, got {type(r.passed)}"


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _format_trace_for_diagnostics(trace: dict) -> str:
    """Format a trace dict for human-readable pytest failure output."""
    lines = [
        f"  subagents_called:  {trace.get('subagents_called', [])}",
        f"  tool_calls:        {[tc.get('name') for tc in trace.get('tool_calls', [])]}",
        f"  hitl_triggered:    {trace.get('hitl_triggered', False)}",
        f"  chat_paused:       {trace.get('chat_paused', False)}",
        f"  final_message:     {str(trace.get('final_message', ''))[:300]}",
    ]
    return "\n".join(lines)
