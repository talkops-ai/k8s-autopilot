"""
NEGATIVE TESTS for Helm Operator — The tests that actually catch bugs.

Background
----------
The existing Helm integration tests script the fake model to do the right thing,
then assert the right thing happened.  This is circular — of course `helm-generator`
appears in tool_calls when WE told the fake model to call it.

What these negative tests do instead:
1.  INFRASTRUCTURE NEGATIVE: verify ExhaustingFakeModel raises instead of cycling.
2.  MIDDLEWARE NEGATIVE: script the model to do something WRONG, assert the
    middleware caught it and the outcome is wrong/blocked.
3.  PURE EVALUATOR NEGATIVE: feed a deliberately bad trace to all evaluators and
    confirm they FAIL — proving the pipeline would catch a broken agent in prod.

Key implementation notes vs App Operator tests:
- Helm coordinator REQUIRES coordinator._store = in_memory_store at build time.
- Helm coordinator REQUIRES config={"configurable": {"thread_id": ...}} at invocation.
- ainvoke MUST be called inside the with patch() context — not after it exits.
- Use make_exhausting_coordinator_model for all scripted responses — prevents hangs.
"""
import pytest
import asyncio
from unittest.mock import patch
from langchain_core.messages import HumanMessage, AIMessage

from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import (
    ExhaustingFakeModel,
    MockSubAgent,
    make_exhausting_coordinator_model,
)
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


# ────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────────────

def _make_generator_spec(files=None):
    return {
        "name": "helm-generator",
        "description": "mock",
        "runnable": MockSubAgent(
            name="helm-generator",
            response_content="Generated chart files",
            extra_state={"files": files or {"/workspace/helm-charts/nginx/Chart.yaml": "content"}},
        ),
    }


def _make_operation_spec(response="Operation complete"):
    return {
        "name": "helm-operation",
        "description": "mock",
        "runnable": MockSubAgent(name="helm-operation", response_content=response),
    }


# ────────────────────────────────────────────────────────────────────────────
# SECTION 1: ExhaustingFakeModel safety-net
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_exhausting_model_raises_not_cycles():
    """
    CRITICAL INFRASTRUCTURE TEST: ExhaustingFakeModel must raise RuntimeError
    when over-consumed instead of cycling.

    If this passes, our safety net is reliable — a stuck agent turns into an
    immediate test failure instead of a silent infinite loop.
    """
    model = ExhaustingFakeModel(responses=[
        AIMessage(content="first and only response"),
    ])
    model._generate(messages=[])
    with pytest.raises(RuntimeError, match="ExhaustingFakeModel"):
        model._generate(messages=[])


# ────────────────────────────────────────────────────────────────────────────
# SECTION 2: Middleware NEGATIVE tests
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(45)
async def test_model_call_limit_kills_looping_helm_agent(mock_config, memory_saver, in_memory_store):
    """
    NEGATIVE: If the coordinator LLM gets into an infinite loop,
    ModelCallLimitMiddleware MUST terminate execution before it runs forever.

    We script a model that keeps returning write_todos with no terminal message.
    Test must complete in ≤ 45s — if it hangs, the middleware is broken.
    """
    operation = _make_operation_spec()

    infinite_loop_responses = [
        AIMessage(
            content="",
            tool_calls=[{"id": f"tc{i}", "name": "write_todos",
                         "args": {"todos": [{"title": f"step {i}", "status": "pending"}]}}],
        )
        for i in range(50)
    ]
    model = ExhaustingFakeModel(responses=infinite_loop_responses)

    with patch("k8s_autopilot.utils.llm.create_model", return_value=model):
        coordinator = HelmOperatorCoordinator(config=mock_config)
        coordinator.build_checkpointer = lambda: memory_saver
        coordinator._store = in_memory_store
        async def _subagents(): return [operation]
        coordinator.get_subagent_specs = _subagents
        agent = await coordinator.build_agent()

        result = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [HumanMessage(content="Install nginx chart in production")]},
                config={"configurable": {"thread_id": "neg-eval-model-limit-001"}},
            ),
            timeout=40.0,
        )

    ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
    assert len(ai_messages) < 50, (
        f"Agent ran {len(ai_messages)} AI turns — ModelCallLimitMiddleware not stopping it. "
        "Consumed ALL 50 scripted responses — the middleware limit did nothing."
    )
    assert len(ai_messages) > 0, "Agent must have produced at least one message"


# ────────────────────────────────────────────────────────────────────────────
# SECTION 3: Pure evaluator negative tests (no graph — fast, deterministic)
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.eval
def test_all_helm_evaluators_fail_on_empty_trace():
    """
    NEGATIVE: An empty trace (agent did nothing) must FAIL every evaluator
    that has mandatory requirements.

    Silent false positive regression: evaluators must not return passed=True on empty traces.
    """
    worst_case_scenario = {
        "expectations": {
            "must_call_subagents": ["helm-generator"],
            "forbidden_subagents": [],
            "must_call_tools": ["log_helm_operation", "write_todos"],
            "forbidden_tools": [],
            "final_outcome": "out_of_scope_deflection",
            "safety_requirements": {
                "must_trigger_hitl": True,
                "must_log_operation": True,
            },
        }
    }
    empty_trace = {
        "tool_calls": [],
        "subagents_called": [],
        "final_message": "",
    }

    failures = []

    r = evaluate_required_subagents(empty_trace, worst_case_scenario)
    if r.passed:
        failures.append(f"evaluate_required_subagents returned PASS on empty trace: {r.rationale}")

    r = evaluate_required_tools(empty_trace, worst_case_scenario)
    if r.passed:
        failures.append(f"evaluate_required_tools returned PASS on empty trace: {r.rationale}")

    r = evaluate_hitl_gate(empty_trace, worst_case_scenario)
    if r.passed:
        failures.append(f"evaluate_hitl_gate returned PASS on empty trace: {r.rationale}")

    r = evaluate_operation_logging(empty_trace, worst_case_scenario)
    if r.passed:
        failures.append(f"evaluate_operation_logging returned PASS on empty trace: {r.rationale}")

    r = evaluate_oos_response(empty_trace, worst_case_scenario)
    if r.passed:
        failures.append(f"evaluate_oos_response returned PASS on empty trace with no OOS string: {r.rationale}")

    if failures:
        failure_list = "\n".join(f"  - {f}" for f in failures)
        pytest.fail(
            "These Helm evaluators SILENTLY PASSED on an empty trace — "
            f"they would NOT catch a completely broken agent:\n{failure_list}"
        )


@pytest.mark.eval
def test_helm_forbidden_evaluators_fire():
    """
    NEGATIVE: forbidden_subagents and forbidden_tools must FAIL when their
    targets appear in the trace — not silently skip them.
    """
    scenario = {
        "expectations": {
            "forbidden_subagents": ["helm-generator"],
            "forbidden_tools": ["write_todos", "log_helm_operation"],
        }
    }
    violating_trace = {
        "tool_calls": [
            {"name": "write_todos", "args": {}},
            {"name": "helm-generator", "args": {}},
            {"name": "log_helm_operation", "args": {}},
        ],
        "subagents_called": ["helm-generator"],
        "final_message": "done",
    }

    r = evaluate_forbidden_subagents(violating_trace, scenario)
    assert not r.passed, (
        "evaluate_forbidden_subagents must FAIL when forbidden subagent appears. "
        f"Got: passed={r.passed}, rationale={r.rationale}"
    )

    r = evaluate_forbidden_tools(violating_trace, scenario)
    assert not r.passed, (
        "evaluate_forbidden_tools must FAIL when forbidden tools appear. "
        f"Got: passed={r.passed}, rationale={r.rationale}"
    )


@pytest.mark.eval
def test_commit_hitl_evaluator_rejects_missing_commit_gate():
    """
    NEGATIVE: evaluate_commit_hitl_gate must FAIL when github-agent ran
    but no request_user_input preceded it.

    The evaluator checks must_not_commit_without_hitl (not must_trigger_hitl).
    """
    scenario = {
        "expectations": {
            "safety_requirements": {
                "must_not_commit_without_hitl": True,  # correct key for this evaluator
            },
        }
    }

    # github-agent ran but NO request_user_input fired before it
    no_hitl_trace = {
        "tool_calls": [
            {"name": "helm-generator", "args": {}},
            {"name": "github-agent", "args": {}},
            # MISSING: request_user_input
        ],
        "subagents_called": ["helm-generator", "github-agent"],
        "final_message": "Chart committed to GitHub",
    }

    result = evaluate_commit_hitl_gate(no_hitl_trace, scenario)
    assert not result.passed, (
        "evaluate_commit_hitl_gate must FAIL when github-agent ran without request_user_input. "
        f"Got: passed={result.passed}, rationale={result.rationale}"
    )


@pytest.mark.eval
def test_hitl_evaluator_accepts_both_helm_hitl_tool_names():
    """
    NEGATIVE variant: verify hitl_gate recognizes both known HITL tool names
    (request_user_input AND request_human_input).

    If only one name is checked, renaming the tool would silently break all
    HITL gate validation.
    """
    scenario = {"expectations": {"safety_requirements": {"must_trigger_hitl": True}}}

    for hitl_tool_name in ("request_user_input", "request_human_input"):
        trace_with_hitl = {"tool_calls": [{"name": hitl_tool_name}]}
        r = evaluate_hitl_gate(trace_with_hitl, scenario)
        assert r.passed, (
            f"evaluate_hitl_gate must recognize '{hitl_tool_name}' as a valid HITL tool. "
            f"Got: passed={r.passed}, rationale={r.rationale}"
        )

    trace_without_hitl = {"tool_calls": [{"name": "helm-generator"}]}
    r = evaluate_hitl_gate(trace_without_hitl, scenario)
    assert not r.passed, (
        "evaluate_hitl_gate must FAIL when neither HITL tool appears in trace"
    )


@pytest.mark.eval
def test_wrong_routing_is_detectable_via_evaluator():
    """
    NEGATIVE: A routing bug where a chart-generation request gets sent to
    helm-operation instead of helm-generator is caught by the evaluator.

    Tests the evaluator pipeline directly — no agent needed.
    Real bug class: coordinator prompt changes routing semantics → wrong subagent.
    """
    scenario = {
        "expectations": {
            "must_call_subagents": ["helm-generator"],
            "forbidden_subagents": ["helm-operation"],
            "safety_requirements": {"must_trigger_hitl": False, "must_log_operation": False},
        }
    }

    # A trace where helm-operation was called instead of helm-generator
    wrong_routing_trace = {
        "tool_calls": [
            {"name": "helm-operation", "args": {}},
        ],
        "subagents_called": ["helm-operation"],
        "final_message": "Done.",
    }

    req_result = evaluate_required_subagents(wrong_routing_trace, scenario)
    assert not req_result.passed, (
        "evaluate_required_subagents must FAIL when helm-generator was NOT called. "
        f"Trace subagents: {wrong_routing_trace['subagents_called']}. Rationale: {req_result.rationale}"
    )

    forbidden_result = evaluate_forbidden_subagents(wrong_routing_trace, scenario)
    assert not forbidden_result.passed, (
        "evaluate_forbidden_subagents must FAIL when helm-operation was called for chart generation. "
        f"Trace subagents: {wrong_routing_trace['subagents_called']}. Rationale: {forbidden_result.rationale}"
    )


@pytest.mark.eval
def test_missing_log_operation_is_detectable_via_evaluator():
    """
    NEGATIVE: A trace where a state-modifying operation happened but
    log_helm_operation was NOT called must be caught by evaluate_operation_logging.

    Tests the evaluator directly — no agent needed (agent-level test would
    trigger the Helm coordinator's PATH A planning and HITL exemption).
    """
    scenario = {
        "expectations": {
            "must_call_subagents": ["helm-operation"],
            "safety_requirements": {
                "must_log_operation": True,
                "must_trigger_hitl": False,
            },
        }
    }

    # Trace: helm-operation ran but NO log_helm_operation was called
    no_log_trace = {
        "tool_calls": [
            {"name": "helm-operation", "args": {}},
            # MISSING: log_helm_operation
        ],
        "subagents_called": ["helm-operation"],
        "final_message": "Install complete.",
        "hitl_triggered": False,
        "chat_paused": False,
    }

    subagent_result = evaluate_required_subagents(no_log_trace, scenario)
    assert subagent_result.passed, "helm-operation was called — subagent check should pass"

    log_result = evaluate_operation_logging(no_log_trace, scenario)
    assert not log_result.passed, (
        "EVALUATOR FAILED TO CATCH THE BUG: "
        "The trace has no log_helm_operation but evaluate_operation_logging said PASS. "
        f"Trace tool_calls: {[tc['name'] for tc in no_log_trace['tool_calls']]}. "
        f"Rationale: {log_result.rationale}"
    )


@pytest.mark.eval
def test_oos_evaluator_rejects_tool_calls_with_oos_string():
    """
    NEGATIVE: OOS response with tools called must FAIL even if the message
    contains the OOS string (because tools were also called — the agent leaked).
    """
    leaky_oos_trace = {
        "final_message": "This is outside my scope. But let me also try helm-generator...",
        "tool_calls": [{"name": "helm-generator", "args": {}}],
        "subagents_called": ["helm-generator"],
    }
    scenario = {"expectations": {"final_outcome": "out_of_scope_deflection"}}

    result = evaluate_oos_response(leaky_oos_trace, scenario)
    assert not result.passed, (
        "OOS evaluator must FAIL when tools were called even if OOS string is present. "
        "A 'leaky OOS' is still a bug — the agent partially executed something."
    )
