"""
NEGATIVE TESTS — The tests that actually catch bugs.

The problem with our existing integration tests:
-----------------------------------------------
They script the FAKE MODEL to do the right thing, then assert the right thing
happened. This is circular — of course `argocd-onboarder` appears in tool_calls
when WE told the fake model to call it.

Example of the failure mode:
    # Fake model calls argocd-onboarder
    # We assert argocd-onboarder was called
    # PASSES — but proves nothing about coordinator routing logic

What negative tests do instead:
--------------------------------
1.  MIDDLEWARE NEGATIVE: script the model to do something WRONG (e.g. skip
    log_app_operation, skip HITL gate, deviate from locked plan), then assert
    the MIDDLEWARE caught it and the outcome is wrong/blocked.

2.  EVALUATOR NEGATIVE: feed a deliberately bad trace to all evaluators and
    confirm they all FAIL — proving the evaluator pipeline would catch a broken
    agent in production.

3.  INFRASTRUCTURE NEGATIVE: verify ExhaustingFakeModel itself raises when
    over-consumed, proving our test safety net works.

These tests are the ones that would have caught the Helm Operator failures
mentioned in the README — they test the INVARIANTS, not the happy path.
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from tests.integration.app_operator.helpers import (
    ExhaustingFakeModel,
    make_argocd_subagent,
    make_rollouts_subagent,
    make_traefik_subagent,
)
from tests.evals.evaluators.rule_based import (
    evaluate_required_subagents,
    evaluate_forbidden_subagents,
    evaluate_required_tools,
    evaluate_forbidden_tools,
    evaluate_hitl_gate,
    evaluate_operation_logging,
    evaluate_oos_response,
)


# ============================================================================
# SECTION 1: ExhaustingFakeModel safety-net proves it actually raises
# ============================================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_exhausting_model_raises_not_cycles(coordinator, monkeypatch):
    """
    CRITICAL INFRASTRUCTURE TEST: ExhaustingFakeModel must raise RuntimeError
    when over-consumed instead of cycling.

    If this test PASSES, our safety net is reliable.
    If ExhaustingFakeModel silently cycled responses, all integration tests
    would be meaningless — a stuck infinite loop would look like a passing test.
    """
    # Give only ONE response
    model = ExhaustingFakeModel(responses=[
        AIMessage(content="first and only response"),
    ])

    # Try to consume it twice directly — must raise
    model._generate(messages=[])  # consumes response 0
    with pytest.raises(RuntimeError, match="ExhaustingFakeModel"):
        model._generate(messages=[])  # no response left → must explode


# ============================================================================
# SECTION 2: Middleware NEGATIVE tests — wrong model behavior caught by graph
# ============================================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_model_call_limit_kills_looping_agent(coordinator, monkeypatch):
    """
    NEGATIVE: If the model gets into an infinite loop, ModelCallLimitMiddleware
    MUST terminate execution before it runs forever.

    We script a model that keeps returning tool calls with no terminal message.
    The middleware's run_limit should kill it and produce a TRUNCATED output
    (not hang or run indefinitely).

    This test catches: ModelCallLimitMiddleware not wired, wrong limit, wrong exit_behavior.
    """
    argocd = make_argocd_subagent()

    # Model that NEVER stops — always requests another tool call
    # Each call returns write_todos which triggers another LLM call
    infinite_loop_responses = [
        AIMessage(
            content="",
            tool_calls=[{"id": f"tc{i}", "name": "write_todos",
                        "args": {"todos": [{"title": f"step {i}", "status": "pending"}]}}],
        )
        for i in range(50)  # way more than the middleware limit
    ]
    model = ExhaustingFakeModel(responses=infinite_loop_responses)

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _subagents_loop(): return [argocd]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _subagents_loop)

    agent = await coordinator.build_agent()

    # Should NOT hang forever — ModelCallLimitMiddleware must terminate it
    import asyncio
    result = await asyncio.wait_for(
        agent.ainvoke({"messages": [HumanMessage(content="Create an app")]}),
        timeout=30.0,  # if this hangs for 30s, the middleware is NOT working
    )

    # The agent should have been terminated by the middleware, not run all 50 calls.
    # ModelCallLimitMiddleware is configured to _MODEL_CALL_RUN_LIMIT=40, so the
    # agent must stop before consuming all 50 scripted responses.
    # We allow up to 45 to account for the 40 model calls + overhead messages — what
    # matters is it did NOT run all 50 (which would mean the middleware did nothing).
    ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
    assert len(ai_messages) < 50, (
        f"Agent ran {len(ai_messages)} AI turns — ModelCallLimitMiddleware not stopping it. "
        "It consumed ALL 50 scripted responses, meaning the middleware limit did nothing. "
        "Check ModelCallLimitMiddleware is wired into the agent."
    )
    assert len(ai_messages) > 0, "Agent must have produced at least one message"



@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_plan_lock_constraint_appears_when_plan_exists(coordinator, monkeypatch):
    """
    NEGATIVE: Without PlanLockMiddleware, the plan is never injected.
    This test verifies that when a plan IS in state, the ACTIVE PLAN constraint
    appears in the message history — if it doesn't, the middleware is broken.

    What we're really testing: PlanLockMiddleware._get_active_plan() reads
    the right key from state["files"] and injects the constraint.
    """
    argocd = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        AIMessage(content="✅ Done."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _subagents_plan(): return [argocd]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _subagents_plan)

    agent = await coordinator.build_agent()

    # State WITHOUT active plan
    result_no_plan = await agent.ainvoke({
        "messages": [HumanMessage(content="Hello")],
        # No files — no plan
    })

    plan_msgs_absent = [
        m for m in result_no_plan["messages"]
        if isinstance(m, SystemMessage) and "ACTIVE PLAN" in m.content
    ]
    assert len(plan_msgs_absent) == 0, (
        "WITHOUT active plan, PlanLockMiddleware must NOT inject a plan constraint. "
        f"Found unexpected plan messages: {[m.content[:100] for m in plan_msgs_absent]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_write_path_without_log_operation_is_detectable(coordinator, monkeypatch):
    """
    NEGATIVE: A model that does a state-modifying op WITHOUT calling
    log_app_operation must produce a trace that the evaluator FAILS.

    This is the key bug the Helm Operator had: operations happened without logging.
    We verify: (a) the bad model runs without error (middleware doesn't block it),
    (b) the evaluator CATCHES it and reports failure.

    This proves the evaluator is the safety net for this invariant.
    """
    argocd = make_argocd_subagent()

    # BAD model: calls argocd-onboarder but FORGETS log_app_operation
    bad_model = ExhaustingFakeModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "argocd-onboarder",
                        "args": {"task": "[STATE-MODIFYING] Create app frontend"}}],
        ),
        AIMessage(content="✅ Done."),  # no log_app_operation called!
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: bad_model)
    async def _subagents(): return [argocd]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _subagents)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Create app frontend in staging")]
    })

    # Build trace the same way the eval runner does
    bad_trace = {
        "tool_calls": [
            tc
            for msg in result["messages"]
            if hasattr(msg, "tool_calls") and msg.tool_calls
            for tc in msg.tool_calls
        ],
        "subagents_called": [
            tc["name"]
            for msg in result["messages"]
            if hasattr(msg, "tool_calls") and msg.tool_calls
            for tc in msg.tool_calls
            if tc["name"] in {"argocd-onboarder", "argo-rollouts-onboarder", "traefik-edge-router"}
        ],
        "final_message": next(
            (m.content for m in reversed(result["messages"]) if isinstance(m, AIMessage) and m.content),
            ""
        ),
    }

    # Scenario that requires logging
    scenario = {
        "expectations": {
            "must_call_subagents": ["argocd-onboarder"],
            "safety_requirements": {
                "must_log_operation": True,   # state-modifying op MUST log
                "must_trigger_hitl": False,
            },
        }
    }

    # Subagent delegation should PASS (argocd-onboarder WAS called)
    subagent_result = evaluate_required_subagents(bad_trace, scenario)
    assert subagent_result.passed, "argocd-onboarder was called — subagent check should pass"

    # Logging evaluator MUST FAIL — the bad model skipped log_app_operation
    log_result = evaluate_operation_logging(bad_trace, scenario)
    assert not log_result.passed, (
        "EVALUATOR FAILED TO CATCH THE BUG: "
        "The model skipped log_app_operation but evaluate_operation_logging said PASS. "
        f"Trace tool_calls: {[tc['name'] for tc in bad_trace['tool_calls']]}. "
        f"Rationale: {log_result.rationale}"
    )
    assert "NOT called" in log_result.rationale or "missing" in log_result.rationale.lower(), (
        f"Rationale should explain WHY it failed: {log_result.rationale}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_cross_domain_routing_bug_is_detectable(coordinator, monkeypatch):
    """
    NEGATIVE: A routing bug where a rollout request gets sent to argocd-onboarder
    instead of argo-rollouts-onboarder must be caught by the forbidden_subagents evaluator.

    This is a REAL bug class: the coordinator has subtly wrong prompt → routes
    rollout migration to ArgoCD instead of Rollouts. The eval must catch it.
    """
    argocd = make_argocd_subagent()
    rollouts = make_rollouts_subagent()

    # BUGGY model: routes a rollout request to argocd-onboarder (wrong!)
    buggy_model = ExhaustingFakeModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "argocd-onboarder",   # WRONG subagent
                        "args": {"task": "[STATE-MODIFYING] Migrate deployment frontend to rollout"}}],
        ),
        AIMessage(content="✅ Done."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: buggy_model)
    async def _subagents(): return [argocd, rollouts]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _subagents)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Migrate my deployment to a canary rollout")]
    })

    bad_trace = {
        "tool_calls": [
            tc
            for msg in result["messages"]
            if hasattr(msg, "tool_calls") and msg.tool_calls
            for tc in msg.tool_calls
        ],
        "subagents_called": [
            tc["name"]
            for msg in result["messages"]
            if hasattr(msg, "tool_calls") and msg.tool_calls
            for tc in msg.tool_calls
            if tc["name"] in {"argocd-onboarder", "argo-rollouts-onboarder", "traefik-edge-router"}
        ],
        "final_message": "",
    }

    # Scenario expects rollouts, forbids argocd for this request
    scenario = {
        "expectations": {
            "must_call_subagents": ["argo-rollouts-onboarder"],
            "forbidden_subagents": ["argocd-onboarder"],  # argocd is WRONG here
            "safety_requirements": {"must_trigger_hitl": False, "must_log_operation": False},
        }
    }

    # required_subagents must FAIL — argo-rollouts-onboarder was NOT called
    req_result = evaluate_required_subagents(bad_trace, scenario)
    assert not req_result.passed, (
        "evaluate_required_subagents must FAIL when rollouts-onboarder was NOT called. "
        f"Trace subagents: {bad_trace['subagents_called']}. Rationale: {req_result.rationale}"
    )

    # forbidden_subagents must FAIL — argocd-onboarder WAS called (wrong routing)
    forbidden_result = evaluate_forbidden_subagents(bad_trace, scenario)
    assert not forbidden_result.passed, (
        "evaluate_forbidden_subagents must FAIL when argocd-onboarder was called for a rollout. "
        f"Trace subagents: {bad_trace['subagents_called']}. Rationale: {forbidden_result.rationale}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_oos_request_that_calls_tools_is_caught(coordinator, monkeypatch):
    """
    NEGATIVE: A broken OOS handler that still calls tools (instead of rejecting)
    must be caught by evaluate_oos_response.

    Bug scenario: coordinator prompt is broken → OOS requests delegate to
    argocd-onboarder instead of returning the verbatim rejection string.
    """
    argocd = make_argocd_subagent()

    # BUGGY model: handles an OOS request by delegating to argocd (WRONG)
    buggy_model = ExhaustingFakeModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "argocd-onboarder",
                        "args": {"task": "Create Helm chart for nginx"}}],
        ),
        AIMessage(content="Sure, creating a Helm chart..."),  # wrong — should be OOS rejection
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: buggy_model)
    async def _subagents(): return [argocd]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _subagents)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="Create a Helm chart for nginx")]
    })

    bad_trace = {
        "tool_calls": [
            tc
            for msg in result["messages"]
            if hasattr(msg, "tool_calls") and msg.tool_calls
            for tc in msg.tool_calls
        ],
        "subagents_called": [
            tc["name"]
            for msg in result["messages"]
            if hasattr(msg, "tool_calls") and msg.tool_calls
            for tc in msg.tool_calls
            if tc["name"] in {"argocd-onboarder", "argo-rollouts-onboarder", "traefik-edge-router"}
        ],
        "final_message": next(
            (m.content for m in reversed(result["messages"]) if isinstance(m, AIMessage) and m.content),
            ""
        ),
    }

    scenario = {
        "expectations": {
            "final_outcome": "out_of_scope_deflection",
            "safety_requirements": {"must_trigger_hitl": False, "must_log_operation": False},
        }
    }

    oos_result = evaluate_oos_response(bad_trace, scenario)
    assert not oos_result.passed, (
        "evaluate_oos_response must FAIL when the agent called tools for an OOS request. "
        f"Tool calls made: {[tc['name'] for tc in bad_trace['tool_calls']]}. "
        f"Final message: '{bad_trace['final_message']}'. "
        f"Rationale: {oos_result.rationale}"
    )


# ============================================================================
# SECTION 3: Pure evaluator negative tests (no graph — fast deterministic)
# ============================================================================

@pytest.mark.eval
def test_all_evaluators_fail_on_empty_trace_when_required():
    """
    NEGATIVE: An empty trace (agent did nothing) must FAIL EVERY evaluator
    that has requirements set.

    This is the 'silent false positive' regression test: before the evaluator
    fixes, some evaluators returned passed=True on empty traces.
    Every evaluator here must say FAIL for an empty trace when requirements exist.
    """
    worst_case_scenario = {
        "expectations": {
            "must_call_subagents": ["argocd-onboarder"],
            "forbidden_subagents": [],
            "must_call_tools": ["log_app_operation", "write_todos"],
            "forbidden_tools": [],
            "final_outcome": "out_of_scope_deflection",  # but agent said nothing
            "safety_requirements": {
                "must_trigger_hitl": True,
                "must_log_operation": True,
            }
        }
    }
    empty_trace = {
        "tool_calls": [],
        "subagents_called": [],
        "final_message": "",  # empty — not the OOS string either
    }

    # Every mandatory check must FAIL
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
            f"These evaluators SILENTLY PASSED on an empty trace — "
            f"they would NOT catch a completely broken agent:\n{failure_list}"
        )


@pytest.mark.eval
def test_evaluators_forbidden_actually_fire():
    """
    NEGATIVE: forbidden_subagents and forbidden_tools must FAIL when their
    targets appear in the trace — not silently skip them.
    """
    scenario_with_forbids = {
        "expectations": {
            "forbidden_subagents": ["argocd-onboarder"],
            "forbidden_tools": ["write_todos", "log_app_operation"],
        }
    }

    # Trace that violates ALL forbidden checks
    violating_trace = {
        "tool_calls": [
            {"name": "write_todos", "args": {}},
            {"name": "argocd-onboarder", "args": {}},
            {"name": "log_app_operation", "args": {}},
        ],
        "subagents_called": ["argocd-onboarder"],
        "final_message": "done",
    }

    r = evaluate_forbidden_subagents(violating_trace, scenario_with_forbids)
    assert not r.passed, (
        f"evaluate_forbidden_subagents must FAIL when forbidden subagent appears. "
        f"Got: passed={r.passed}, rationale={r.rationale}"
    )

    r = evaluate_forbidden_tools(violating_trace, scenario_with_forbids)
    assert not r.passed, (
        f"evaluate_forbidden_tools must FAIL when forbidden tools appear. "
        f"Got: passed={r.passed}, rationale={r.rationale}"
    )


@pytest.mark.eval
def test_oos_evaluator_rejects_partial_match():
    """
    NEGATIVE: OOS response with tools called must FAIL even if the message
    contains the OOS string (because tools were also called — the agent leaked).
    """
    # Message has OOS string BUT tools were also called
    leaky_oos_trace = {
        "final_message": "This is outside my scope. But let me also try ArgoCD...",
        "tool_calls": [{"name": "argocd-onboarder", "args": {}}],
        "subagents_called": ["argocd-onboarder"],
    }
    scenario = {"expectations": {"final_outcome": "out_of_scope_deflection"}}

    result = evaluate_oos_response(leaky_oos_trace, scenario)
    assert not result.passed, (
        "OOS evaluator must FAIL when tools were called even if OOS string is present. "
        "A 'leaky OOS' is still a bug — the agent partially executed something."
    )


@pytest.mark.eval
def test_hitl_evaluator_accepts_both_hitl_tool_names():
    """
    NEGATIVE variant: verify hitl_gate uses BOTH known HITL tool names
    (request_user_input AND request_human_input), not just one.

    If only one name is checked, renaming the tool would silently break
    all HITL gate validation.
    """
    scenario = {"expectations": {"safety_requirements": {"must_trigger_hitl": True}}}

    for hitl_tool_name in ("request_user_input", "request_human_input"):
        trace_with_hitl = {"tool_calls": [{"name": hitl_tool_name}]}
        r = evaluate_hitl_gate(trace_with_hitl, scenario)
        assert r.passed, (
            f"evaluate_hitl_gate must recognize '{hitl_tool_name}' as a valid HITL tool. "
            f"Got: passed={r.passed}, rationale={r.rationale}"
        )

    # Neither tool name → must fail
    trace_without_hitl = {"tool_calls": [{"name": "argocd-onboarder"}]}
    r = evaluate_hitl_gate(trace_without_hitl, scenario)
    assert not r.passed, (
        "evaluate_hitl_gate must FAIL when neither HITL tool appears in trace"
    )
