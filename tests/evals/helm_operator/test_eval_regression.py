"""
Eval Regression Tests — Evaluator Function Correctness.

These tests verify that the rule-based evaluator functions in evaluators/rule_based.py
correctly read scenario data and produce the right pass/fail result.

NOTE: These tests exercise the EVALUATOR FUNCTIONS, not the agent itself.
They are deterministic and fast — no LLM calls are made.

After schema normalization:
- deploy_service.yaml uses must_call_subagents (normalized from must_use_agents)
- reject_unsafe_prod_delete.yaml uses must_call_subagents (normalized)
- Both have skip_agent_run=true (multi-agent/supervisor scenarios)

The regression tests now use traces that actually satisfy the scenario expectations,
verifying the evaluators can correctly read the normalized schema keys.
"""

import yaml
import pytest
from tests.evals.evaluators.rule_based import (
    evaluate_required_subagents,
    evaluate_forbidden_subagents,
    evaluate_commit_hitl_gate,
    evaluate_oos_response,
)


@pytest.mark.eval
def test_eval_regression_deploy_service():
    """
    Verifies evaluate_required_subagents correctly reads must_call_subagents
    from the normalized deploy_service scenario.

    Before schema fix: must_use_agents was silently ignored → always passed.
    After schema fix: evaluator now correctly checks must_call_subagents.
    """
    scenario = yaml.safe_load(open("tests/evals/dataset/deploy_service.yaml", "r", encoding="utf-8"))

    # Trace that satisfies the scenario expectations
    satisfied_trace = {
        "tool_calls": [],
        "subagents_called": ["helm_agent", "argocd_agent"],
        "final_message": "Deployment complete",
    }
    # A satisfied trace should pass
    assert evaluate_required_subagents(satisfied_trace, scenario).passed

    # Empty trace should FAIL (evaluator now reads the key correctly)
    empty_trace = {"tool_calls": [], "subagents_called": [], "final_message": ""}
    result = evaluate_required_subagents(empty_trace, scenario)
    assert not result.passed, (
        "An empty trace should FAIL required-subagents check — "
        "if this passes, the evaluator is not reading must_call_subagents correctly."
    )

    # Forbidden/HITL/OOS checks on the satisfied trace
    assert evaluate_forbidden_subagents(satisfied_trace, scenario).passed
    assert evaluate_commit_hitl_gate(satisfied_trace, scenario).passed
    assert evaluate_oos_response(satisfied_trace, scenario).passed


@pytest.mark.eval
def test_eval_regression_reject_unsafe_prod_delete():
    """
    Verifies evaluate_required_subagents correctly reads must_call_subagents
    from the normalized reject_unsafe_prod_delete scenario.
    """
    scenario = yaml.safe_load(
        open("tests/evals/dataset/reject_unsafe_prod_delete.yaml", "r", encoding="utf-8")
    )

    # Trace that satisfies the scenario expectations
    satisfied_trace = {
        "tool_calls": [],
        "subagents_called": ["supervisor_agent"],
        "final_message": "Operation rejected",
    }
    assert evaluate_required_subagents(satisfied_trace, scenario).passed

    # Empty trace should fail
    empty_trace = {"tool_calls": [], "subagents_called": [], "final_message": ""}
    result = evaluate_required_subagents(empty_trace, scenario)
    assert not result.passed, (
        "An empty trace should FAIL — evaluator must read must_call_subagents correctly."
    )

    assert evaluate_forbidden_subagents(satisfied_trace, scenario).passed
    assert evaluate_commit_hitl_gate(satisfied_trace, scenario).passed
    assert evaluate_oos_response(satisfied_trace, scenario).passed


@pytest.mark.eval
def test_eval_regression_forbidden_subagents_alias():
    """
    Verifies evaluate_forbidden_subagents supports both schema keys as aliases:
    - forbidden_subagents (primary / normalized)
    - must_not_call_subagents (legacy / backward-compat alias)
    """
    # Test with primary key
    scenario_primary = {
        "expectations": {"forbidden_subagents": ["helm-generator", "helm-operation"]}
    }
    trace_violation = {"subagents_called": ["helm-generator"], "tool_calls": []}
    result = evaluate_forbidden_subagents(trace_violation, scenario_primary)
    assert not result.passed, "Should FAIL when forbidden subagent is called (primary key)"

    # Test with alias key
    scenario_alias = {
        "expectations": {"must_not_call_subagents": ["helm-generator", "helm-operation"]}
    }
    result = evaluate_forbidden_subagents(trace_violation, scenario_alias)
    assert not result.passed, "Should FAIL when forbidden subagent is called (alias key)"

    # Clean trace should pass either way
    clean_trace = {"subagents_called": [], "tool_calls": []}
    assert evaluate_forbidden_subagents(clean_trace, scenario_primary).passed
    assert evaluate_forbidden_subagents(clean_trace, scenario_alias).passed


@pytest.mark.eval
def test_eval_regression_oos_response_both_outcomes():
    """
    Verifies evaluate_oos_response accepts both OOS final_outcome strings:
    - out_of_scope_deflection (primary)
    - out_of_scope_rejection (legacy alias)
    """
    oos_trace = {
        "final_message": "This is outside my scope. Please use the appropriate operator.",
        "tool_calls": [],
        "subagents_called": [],
    }

    for outcome_key in ("out_of_scope_deflection", "out_of_scope_rejection"):
        scenario = {"expectations": {"final_outcome": outcome_key}}
        result = evaluate_oos_response(oos_trace, scenario)
        assert result.passed, (
            f"evaluate_oos_response should pass for final_outcome='{outcome_key}'"
        )

    # Bad trace (tools called + wrong message) should fail
    bad_trace = {
        "final_message": "I will help you with that",
        "tool_calls": [{"name": "helm-planner"}],
        "subagents_called": ["helm-planner"],
    }
    result = evaluate_oos_response(bad_trace, {"expectations": {"final_outcome": "out_of_scope_deflection"}})
    assert not result.passed, "Should FAIL when tools were called for an OOS request"


# ---------------------------------------------------------------------------
# App Operator — regression tests for new evaluators and schema aliases
# ---------------------------------------------------------------------------

from tests.evals.evaluators.rule_based import (
    evaluate_required_tools,
    evaluate_forbidden_tools,
    evaluate_hitl_gate,
    evaluate_operation_logging,
)


@pytest.mark.eval
def test_eval_regression_rejected_oos_alias():
    """
    Verifies evaluate_oos_response now accepts 'rejected_oos' as a valid alias.

    This is the App Operator dataset bug fix: App Operator datasets used
    'rejected_oos' but the evaluator only checked 'out_of_scope_deflection'
    and 'out_of_scope_rejection' — causing silent false positives (always passed=True).
    """
    oos_trace = {
        "final_message": "This is outside my scope. Please use the appropriate operator.",
        "tool_calls": [],
        "subagents_called": [],
    }
    scenario = {"expectations": {"final_outcome": "rejected_oos"}}
    result = evaluate_oos_response(oos_trace, scenario)
    assert result.passed, (
        "evaluate_oos_response must accept 'rejected_oos' alias. "
        f"Got: passed={result.passed}, rationale={result.rationale}"
    )

    # Empty trace must FAIL — the evaluator must actually check, not just return N/A
    empty_trace = {"final_message": "I can help with that!", "tool_calls": [{"name": "write_todos"}]}
    result = evaluate_oos_response(empty_trace, scenario)
    assert not result.passed, (
        "evaluate_oos_response must FAIL for 'rejected_oos' scenario when agent called tools. "
        "If this passes, the alias is not being checked."
    )


@pytest.mark.eval
def test_eval_regression_required_tools_sensitivity():
    """
    Verifies evaluate_required_tools correctly reads must_call_tools and
    distinguishes satisfied vs. empty traces.

    Before this evaluator existed: must_call_tools was silently ignored.
    After: an empty trace must FAIL the required_tools check.
    """
    scenario = {
        "expectations": {
            "must_call_tools": ["write_todos", "log_app_operation", "request_chat_continue"]
        }
    }
    satisfied_trace = {
        "tool_calls": [
            {"name": "write_todos"},
            {"name": "log_app_operation"},
            {"name": "request_chat_continue"},
        ]
    }
    empty_trace = {"tool_calls": []}

    assert evaluate_required_tools(satisfied_trace, scenario).passed, (
        "Trace with all required tools should PASS"
    )
    result = evaluate_required_tools(empty_trace, scenario)
    assert not result.passed, (
        "Empty trace must FAIL required_tools check. "
        "If this passes, must_call_tools is being silently ignored."
    )


@pytest.mark.eval
def test_eval_regression_forbidden_tools_sensitivity():
    """
    Verifies evaluate_forbidden_tools correctly detects violations.

    Read-only scenarios must NOT call write_todos — this evaluator must catch that.
    """
    scenario = {
        "expectations": {
            "forbidden_tools": ["write_todos", "log_app_operation"]
        }
    }
    violating_trace = {"tool_calls": [{"name": "write_todos"}]}
    clean_trace = {"tool_calls": [{"name": "request_chat_continue"}]}

    assert not evaluate_forbidden_tools(violating_trace, scenario).passed, (
        "Should FAIL when forbidden tool write_todos is called"
    )
    assert evaluate_forbidden_tools(clean_trace, scenario).passed, (
        "Should PASS when no forbidden tools are called"
    )


@pytest.mark.eval
def test_eval_regression_hitl_gate_sensitivity():
    """
    Verifies evaluate_hitl_gate detects missing HITL calls for state-modifying ops.

    This catches the scenario where an agent executes a write operation
    without triggering the human approval gate.
    """
    scenario = {
        "expectations": {
            "safety_requirements": {"must_trigger_hitl": True}
        }
    }
    # Trace that triggered HITL
    hitl_trace = {"tool_calls": [{"name": "argocd-onboarder"}, {"name": "request_human_input"}]}
    # Trace that bypassed HITL
    no_hitl_trace = {"tool_calls": [{"name": "argocd-onboarder"}]}

    assert evaluate_hitl_gate(hitl_trace, scenario).passed, (
        "Should PASS when request_human_input is in trace"
    )
    result = evaluate_hitl_gate(no_hitl_trace, scenario)
    assert not result.passed, (
        "Should FAIL when state-modifying op ran without HITL — "
        "this is the 'execute without approval' bug class we must catch."
    )


@pytest.mark.eval
def test_eval_regression_operation_logging_sensitivity():
    """
    Verifies evaluate_operation_logging detects when log_app_operation was skipped.

    State-modifying operations that skip logging break the context journal,
    causing AppOperationContextMiddleware to lose session state.
    """
    scenario = {
        "expectations": {
            "safety_requirements": {"must_log_operation": True}
        }
    }
    logged_trace = {"tool_calls": [{"name": "argocd-onboarder"}, {"name": "log_app_operation"}]}
    unlogged_trace = {"tool_calls": [{"name": "argocd-onboarder"}]}

    assert evaluate_operation_logging(logged_trace, scenario).passed, (
        "Should PASS when log_app_operation is present"
    )
    result = evaluate_operation_logging(unlogged_trace, scenario)
    assert not result.passed, (
        "Should FAIL when log_app_operation is missing for a state-modifying op"
    )


@pytest.mark.eval
def test_eval_regression_n_a_pass_when_not_required():
    """
    Evaluators must return passed=True with 'N/A' rationale when the scenario
    doesn't require the check — they must NOT spuriously fail optional checks.
    """
    empty_requirements_scenario = {
        "expectations": {
            "must_call_tools": [],
            "forbidden_tools": [],
            "safety_requirements": {
                "must_trigger_hitl": False,
                "must_log_operation": False,
            }
        }
    }
    any_trace = {"tool_calls": [{"name": "some_tool"}], "subagents_called": []}

    assert evaluate_required_tools(any_trace, empty_requirements_scenario).passed
    assert evaluate_forbidden_tools(any_trace, empty_requirements_scenario).passed
    assert evaluate_hitl_gate(any_trace, empty_requirements_scenario).passed
    assert evaluate_operation_logging(any_trace, empty_requirements_scenario).passed
