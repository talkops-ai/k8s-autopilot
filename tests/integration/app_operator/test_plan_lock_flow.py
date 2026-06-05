"""
Integration: PlanLockMiddleware — through-graph enforcement test.

These tests verify PlanLockMiddleware injects plan constraints into the
MESSAGE STREAM of a real graph execution — not just the middleware method in
isolation (that's already covered in unit/test_plan_lock_mw.py).

LangChain guidance: integration tests verify components work together.
The middleware must actually affect the agent's message history during ainvoke().
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from tests.integration.app_operator.helpers import ExhaustingFakeModel, make_argocd_subagent
from k8s_autopilot.core.agents.app_operator.middleware import PlanLockMiddleware


def _tool_call_msg(name: str, call_id: str = "tc1", args: dict = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args or {}}],
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_plan_lock_injects_system_message_into_graph(coordinator, monkeypatch):
    """
    When state['files']['/plan/active-plan.md'] is set, PlanLockMiddleware must
    inject a SystemMessage constraint into the agent's message history.

    This is the INTEGRATION test: we verify the SystemMessage actually appears
    in the state returned by the real graph ainvoke(), not just the middleware
    before_model() method.
    """
    mock_subagent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        _tool_call_msg("argocd-onboarder", "tc1", {
            "task": "[STATE-MODIFYING] [PLAN-LOCKED] Create app frontend"
        }),
        AIMessage(content="✅ Done."),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa0(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa0)

    agent = await coordinator.build_agent()

    # Inject an active plan into the initial state (simulates supervisor writing the plan)
    initial_state = {
        "messages": [HumanMessage(content="Execute the approved plan")],
        "files": {
            "/plan/active-plan.md": {
                "content": "## Approved Plan\n- Create app 'frontend' in namespace 'production'\n- Repo: github.com/org/frontend"
            }
        },
    }
    result = await agent.ainvoke(initial_state)

    # Verify a SystemMessage from PlanLockMiddleware appears in the output messages
    system_messages = [
        msg for msg in result["messages"]
        if isinstance(msg, SystemMessage) and "ACTIVE PLAN" in msg.content
    ]
    assert len(system_messages) > 0, (
        "PlanLockMiddleware must inject a SystemMessage with 'ACTIVE PLAN' into the graph. "
        f"Got messages: {[type(m).__name__ for m in result['messages']]}"
    )
    assert "frontend" in system_messages[0].content, (
        "Injected plan constraint must contain the approved plan content"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_plan_lock_no_injection_without_active_plan(coordinator, monkeypatch):
    """
    When no active plan exists in state['files'], PlanLockMiddleware must NOT inject.

    Verifies the middleware is correctly gated on file existence.
    """
    mock_subagent = make_argocd_subagent()

    model = ExhaustingFakeModel(responses=[
        AIMessage(content="How can I help?"),
    ])

    monkeypatch.setattr(coordinator, "get_model", lambda: model)
    async def _sa1(): return [mock_subagent]
    monkeypatch.setattr(coordinator, "get_subagent_specs", _sa1)

    agent = await coordinator.build_agent()
    result = await agent.ainvoke({
        "messages": [HumanMessage(content="What can you do?")]
    })

    plan_messages = [
        msg for msg in result["messages"]
        if isinstance(msg, SystemMessage) and "ACTIVE PLAN" in msg.content
    ]
    assert len(plan_messages) == 0, (
        "PlanLockMiddleware must NOT inject SystemMessage when no active plan is set"
    )
