"""
Prompt Reasoning Tests — Helm Operator Coordinator.

These tests verify that the HELM_COORDINATOR_PROMPT actually causes the LLM to
make the correct routing decisions.  They use the REAL model configured in your
environment (via Config() → .env), but with mocked subagents so no actual Helm
operations occur.

WHY THESE TESTS EXIST:
  FakeMessagesListChatModel bypasses the prompt entirely — it ignores all
  message content and returns pre-scripted responses.  This means unit and
  integration tests can pass even if the coordinator prompt is deleted or
  completely wrong.  These tests close that gap by exercising the actual
  LLM inference path against the prompt.

RUN:
  # Locally (needs GOOGLE_API_KEY / LLM env vars in .env):
  pytest tests/evals/test_prompt_reasoning.py -v -m "eval and slow"

SKIP (CI):
  pytest tests/ -m "not slow"

Each test:
  - Builds a real HelmOperatorCoordinator with Config() from environment
  - Overrides get_subagent_specs() with lightweight MockSubAgent stubs
  - Disables MCP server connections (empty MCP_SERVERS)
  - Invokes the real LLM (no FakeModel)
  - Asserts on the agent's routing decision
"""

import os
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from k8s_autopilot.config.config import Config
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
from tests.integration.helm_operator.fixtures.mock_tools import (
    MockSubAgent,
    get_fake_planner_subagent,
    get_fake_generator_subagent,
    get_fake_validator_valid,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_config():
    """
    Real Config() sourced from environment / .env.

    MCP_SERVERS is overridden to empty so no stdio MCP processes are launched
    during testing.  The coordinator model and all LLM config come from the
    environment — the same source as production.
    """
    cfg = Config({"MCP_SERVERS": []})
    return cfg


@pytest.fixture
def memory_saver():
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


async def _build_coordinator_with_mocked_subagents(real_config, memory_saver, subagent_specs):
    """
    Helper: build a coordinator with the real LLM but mocked subagents.

    This is the key pattern for prompt reasoning tests:
      - Real model  →  real LLM inference, real prompt decisions
      - Mock subagents  →  no real Helm operations, controlled state mutations
    """
    coordinator = HelmOperatorCoordinator(config=real_config)
    coordinator.build_checkpointer = lambda: memory_saver

    async def get_mock_subagent_specs():
        return subagent_specs

    coordinator.get_subagent_specs = get_mock_subagent_specs
    agent = await coordinator.build_agent()
    return agent


def _extract_trace(result):
    """Extract subagents called and final message text from agent result."""
    messages = result.get("messages", [])
    subagents_called = []
    tool_calls_all = []
    final_message = ""

    for msg in messages:
        if isinstance(msg, AIMessage):
            # Gemini returns AIMessage.content as a list of dicts (multi-part).
            # Flatten to a plain string for assertion purposes.
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                ).strip()
            final_message = str(content or "")
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    tool_calls_all.append(name)
                    if name.startswith("helm-") or name in (
                        "github-agent", "requirements_analyser", "architecture_planner"
                    ):
                        subagents_called.append(name)

    return {
        "subagents_called": subagents_called,
        "tool_calls": tool_calls_all,
        "final_message": final_message,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Priority 2 — Prompt Reasoning Tests
# ---------------------------------------------------------------------------

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_oos_query_deflected(real_config, memory_saver):
    """
    SEVERE-02 / Category A: OOS classification with real model.

    The coordinator prompt says:
      "DIFFERENT DOMAIN / OUT-OF-SCOPE TASKS … → Immediately return the
       following string verbatim: 'This is outside my scope. …'"

    Verify that an ArgoCD request is deflected with the OOS string and that
    NO tool calls or subagent delegations are made.

    Production bug caught: if the prompt OOS section is broken or the model
    ignores it, the agent will try to helm-install argocd or call helm-planner.
    """
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")

    fake_planner = get_fake_planner_subagent()
    fake_generator = get_fake_generator_subagent({})
    fake_validator = get_fake_validator_valid()

    agent = await _build_coordinator_with_mocked_subagents(
        real_config,
        memory_saver,
        [fake_planner, fake_generator, fake_validator],
    )

    config = {"configurable": {"thread_id": "reasoning-oos-001"}}
    initial_state = {
        "messages": [HumanMessage(content="Please sync the ArgoCD application for api-server.")],
    }

    result = await agent.ainvoke(initial_state, config=config)
    trace = _extract_trace(result)

    # The prompt requires verbatim OOS string
    assert "outside my scope" in trace["final_message"].lower(), (
        f"Expected OOS deflection string in response, got:\n{trace['final_message']}"
    )

    # No subagents should be called for an OOS query
    assert len(trace["subagents_called"]) == 0, (
        f"OOS query should not call any subagents, but called: {trace['subagents_called']}"
    )

    # Planner and generator should NOT have been invoked
    assert fake_planner["runnable"].calls == 0, (
        "helm-planner should NOT be called for an OOS ArgoCD request"
    )
    assert fake_generator["runnable"].calls == 0, (
        "helm-generator should NOT be called for an OOS ArgoCD request"
    )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_conversational_no_tools(real_config, memory_saver):
    """
    SEVERE-02 / Category A: Conversational classification with real model.

    The coordinator prompt says:
      "CONVERSATIONAL / END-OF-WORKFLOW (e.g., 'thanks', 'done', …)
       → Do NOT call any tools."

    Verify that a 'thanks' message gets a direct polite reply with zero
    tool calls of any kind (not even sync_workspace or request_user_input).

    Production bug caught: if the classification is wrong, "thanks" triggers
    helm-planner and the user gets a confusing chart-generation workflow.
    """
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")

    fake_planner = get_fake_planner_subagent()
    fake_generator = get_fake_generator_subagent({})

    agent = await _build_coordinator_with_mocked_subagents(
        real_config,
        memory_saver,
        [fake_planner, fake_generator, get_fake_validator_valid()],
    )

    config = {"configurable": {"thread_id": "reasoning-conv-001"}}
    initial_state = {
        "messages": [HumanMessage(content="Thanks for your help!")],
    }

    result = await agent.ainvoke(initial_state, config=config)
    trace = _extract_trace(result)

    # Should be a conversational reply — some content expected
    assert trace["final_message"].strip(), (
        "Expected a polite conversational reply, got empty response"
    )

    # No tool calls of any kind
    assert len(trace["tool_calls"]) == 0, (
        f"Conversational 'thanks' should produce zero tool calls, got: {trace['tool_calls']}"
    )

    # Planner and generator must NOT have been called
    assert fake_planner["runnable"].calls == 0, (
        "helm-planner should NOT be called for a conversational 'thanks' message"
    )
    assert fake_generator["runnable"].calls == 0, (
        "helm-generator should NOT be called for a conversational 'thanks' message"
    )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_new_chart_routes_to_planner_first(real_config, memory_saver):
    """
    SEVERE-02 / Category A: New chart routing with real model (no skills pre-loaded).

    The coordinator prompt says:
      "Workflow — New Chart: 1. Check if skills exist … If it does NOT exist:
       task(helm-planner): 'Plan Helm chart for: {request}'"

    Verify that with NO skill files in state, the coordinator calls helm-planner
    BEFORE helm-generator.

    Production bug caught: if the workflow ordering is wrong, the agent skips
    planning and generates a chart without the architecture decision phase,
    producing low-quality or incomplete charts.
    """
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")

    call_order = []

    class OrderTrackingSubAgent(MockSubAgent):
        """Tracks call order across multiple subagents."""
        def __init__(self, name, response_content, extra_state=None):
            super().__init__(name, response_content, extra_state)
            self._order_list = call_order

        async def ainvoke(self, state, config=None, **kwargs):
            self._order_list.append(self.name)
            return await super().ainvoke(state, config=config, **kwargs)

    planner_agent = OrderTrackingSubAgent(name="helm-planner", response_content="Planning complete. Skills written for nginx.")
    generator_agent = OrderTrackingSubAgent(
        name="helm-generator",
        response_content="Generated 7 files",
        extra_state={"files": {"/workspace/helm-charts/nginx/Chart.yaml": "apiVersion: v2"}},
    )
    validator_agent = MockSubAgent(name="helm-validator", response_content="VALID: all checks passed")

    subagent_specs = [
        {"name": "helm-planner", "description": "Plans helm charts", "runnable": planner_agent},
        {"name": "helm-generator", "description": "Generates helm charts", "runnable": generator_agent},
        {"name": "helm-validator", "description": "Validates helm charts", "runnable": validator_agent},
    ]

    agent = await _build_coordinator_with_mocked_subagents(
        real_config, memory_saver, subagent_specs
    )

    config = {"configurable": {"thread_id": "reasoning-new-chart-001"}}
    # No skills pre-loaded in files — coordinator must call helm-planner first
    initial_state = {
        "messages": [HumanMessage(content="Create a production-ready Helm chart for nginx web server")],
    }

    try:
        await agent.ainvoke(initial_state, config=config)
    except Exception:
        # GraphInterrupt at commit gate is expected — that's fine
        pass

    assert planner_agent.calls > 0, (
        "helm-planner must be called when no skills are pre-loaded"
    )
    assert generator_agent.calls > 0, (
        "helm-generator must be called after planning"
    )

    # Verify ordering: planner must precede generator in call sequence
    assert "helm-planner" in call_order, "helm-planner never called"
    assert "helm-generator" in call_order, "helm-generator never called"
    planner_first_idx = next(i for i, n in enumerate(call_order) if n == "helm-planner")
    generator_first_idx = next(i for i, n in enumerate(call_order) if n == "helm-generator")
    assert planner_first_idx < generator_first_idx, (
        f"helm-planner must be called BEFORE helm-generator. "
        f"Call order was: {call_order}"
    )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
async def test_skill_exists_skips_planner(real_config, memory_saver):
    """
    SEVERE-02 / Category A: Skill-exists shortcut with real model.

    The coordinator prompt says:
      "1. Check if skills exist … If it exists: SKIP helm-planner and
       helm-skill-builder entirely. Go directly to step 3."

    Verify that when a skill file is pre-loaded in state, the coordinator goes
    DIRECTLY to helm-generator without calling helm-planner.

    Production bug caught: if the shortcut logic fails, every chart request
    wastes 2-3 extra LLM calls on planning that's already been done, adding
    latency and cost.
    """
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests skipped in CI")

    planner_agent = MockSubAgent(name="helm-planner", response_content="Planning complete")
    generator_agent = MockSubAgent(
        name="helm-generator",
        response_content="Generated 7 files",
        extra_state={"files": {"/workspace/helm-charts/nginx/Chart.yaml": "apiVersion: v2"}},
    )
    validator_agent = MockSubAgent(name="helm-validator", response_content="VALID: all checks passed")

    subagent_specs = [
        {"name": "helm-planner", "description": "Plans helm charts", "runnable": planner_agent},
        {"name": "helm-generator", "description": "Generates helm charts", "runnable": generator_agent},
        {"name": "helm-validator", "description": "Validates helm charts", "runnable": validator_agent},
    ]

    agent = await _build_coordinator_with_mocked_subagents(
        real_config, memory_saver, subagent_specs
    )

    config = {"configurable": {"thread_id": "reasoning-skill-skip-001"}}
    # Pre-load skill file — this is what tells the coordinator to skip helm-planner
    initial_state = {
        "messages": [HumanMessage(content="Create a Helm chart for nginx web server")],
        "files": {
            "/skills/helm-operator/nginx-chart-generator/SKILL.md": {
                "content": "# Nginx Chart Generator Skill\n\nThis skill generates nginx Helm charts."
            }
        },
    }

    try:
        await agent.ainvoke(initial_state, config=config)
    except Exception:
        # GraphInterrupt at commit gate is expected — that's fine
        pass

    # Generator must be called (we still need chart generation)
    assert generator_agent.calls > 0, (
        "helm-generator must be called even when skills exist"
    )

    # --- HARD ASSERTION (middleware guarantees this) ---
    # The skill_exists_shortcut middleware (wrap_model_call) deterministically
    # removes helm-planner from the tool list when skill files exist in
    # state["files"].  The LLM physically cannot call a tool that has been
    # removed.  This was previously a canary warning for a prompt gap —
    # now enforced by middleware.
    #
    # Reference: k8s_autopilot/core/agents/helm_operator/middleware.py
    #            → skill_exists_shortcut @wrap_model_call
    assert planner_agent.calls == 0, (
        f"helm-planner should NOT be called when skill exists "
        f"(middleware removes it from tool list). "
        f"Called {planner_agent.calls} time(s). "
        f"Check that skill_exists_shortcut middleware is wired into "
        f"build_k8s_middleware()."
    )

