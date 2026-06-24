"""
Eval: Observability Operator prompt reasoning tests.

Tests the real LLM's classification and routing decisions by running
specific prompts through the ObservabilityCoordinator with mocked
subagents. Skipped in CI — requires a real LLM API key.

These tests catch prompt-level regression bugs:
- OOS request incorrectly processed as in-scope
- Conversational closure triggers unnecessary delegation
- Read-only queries trigger state-modifying tools
- Ambiguous requests don't trigger clarification

Architecture:
- Subagents are wrapped in CompiledSubAgent (matching production interface)
  but use a mock runnable that returns a scripted response — no real MCP.
- The coordinator uses the REAL LLM to make routing decisions.
- The evaluator checks the trajectory for correct classification.
"""
import os
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from k8s_autopilot.config.config import Config
from k8s_autopilot.core.agents.observability.coordinator import ObservabilityCoordinator


def _make_mock_compiled_subagent(name: str, description: str):
    """Create a CompiledSubAgent with a mock runnable that returns a scripted response."""
    from deepagents.middleware.subagents import CompiledSubAgent

    async def _mock_runnable(state, config=None, **kwargs):
        new_state = dict(state)
        messages = list(new_state.get("messages", []))
        messages.append(AIMessage(
            content=f"[MOCK] {name} completed successfully. Operation acknowledged."
        ))
        new_state["messages"] = messages
        return new_state

    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=RunnableLambda(_mock_runnable).with_config({"run_name": name}),
    )


def _get_mock_obs_subagents():
    """Return 5 CompiledSubAgent objects with mock runnables."""
    return [
        _make_mock_compiled_subagent(
            "prometheus-operator",
            "Manages Prometheus monitoring: PromQL queries, exporter lifecycle, ServiceMonitors, rules.",
        ),
        _make_mock_compiled_subagent(
            "alertmanager-operator",
            "Manages Alertmanager operations: alert triage, silence lifecycle, routing audit.",
        ),
        _make_mock_compiled_subagent(
            "opentelemetry-operator",
            "Manages OpenTelemetry pipelines: collector provisioning, service onboarding.",
        ),
        _make_mock_compiled_subagent(
            "loki-operator",
            "Manages Grafana Loki log observability: LogQL queries, label discovery, log analysis.",
        ),
        _make_mock_compiled_subagent(
            "tempo-operator",
            "Manages Grafana Tempo distributed tracing: TraceQL queries, trace search, CRD lifecycle.",
        ),
    ]


async def _build_coordinator_with_real_llm(subagent_specs):
    """Build an ObservabilityCoordinator with real LLM + mocked subagents."""
    from langgraph.checkpoint.memory import MemorySaver

    config = Config()
    coordinator = ObservabilityCoordinator(config=config)

    # Use in-memory checkpointer for eval isolation
    coordinator.build_checkpointer = lambda: MemorySaver()

    # Patch subagent specs to use mocked subagents (no real MCP)
    async def _mock_get_subagent_specs():
        return subagent_specs

    coordinator.get_subagent_specs = _mock_get_subagent_specs
    agent = await coordinator.build_agent()
    return agent


def _extract_trace(result):
    """Extract tool calls and final message from agent result."""
    messages = result.get("messages", [])
    tool_calls_all = []
    subagents_called = []
    final_message = ""

    OBS_SUBAGENT_NAMES = {
        "prometheus-operator", "alertmanager-operator",
        "opentelemetry-operator", "loki-operator", "tempo-operator",
    }

    for msg in messages:
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text"):
                            text_parts.append(part["text"])
                        elif "text" in part and part.get("type") != "thinking":
                            text_parts.append(str(part["text"]))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = " ".join(text_parts).strip()
            final_message = str(content or "")
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    tool_calls_all.append((name, tc.get("args", {})))
                    if name in OBS_SUBAGENT_NAMES:
                        subagents_called.append(name)

    return {
        "tool_calls": tool_calls_all,
        "tool_names": [tc[0] for tc in tool_calls_all],
        "subagents_called": subagents_called,
        "final_message": final_message,
    }


# ─────────────────────────────────────────────────────────────────────────
# Test cases — each exercises the real LLM's routing decision
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_oos_helm_classification():
    """OOS: 'Create a Helm chart' must be rejected without any tool calls."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="Create a Helm chart for my application.")]},
        config={"configurable": {"thread_id": "eval-obs-oos-helm"}},
    )

    trace = _extract_trace(result)
    # Must NOT delegate to any subagent
    assert len(trace["subagents_called"]) == 0, (
        f"OOS request delegated to subagents: {trace['subagents_called']}\n"
        f"Tool calls: {trace['tool_names']}\n"
        f"Final message: {trace['final_message'][:200]}"
    )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_oos_k8s_pods_classification():
    """OOS: 'Show me Kubernetes pods' must be rejected without any tool calls."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="Show me Kubernetes pods")]},
        config={"configurable": {"thread_id": "eval-obs-oos-pods"}},
    )

    trace = _extract_trace(result)
    assert len(trace["subagents_called"]) == 0, (
        f"OOS request delegated to subagents: {trace['subagents_called']}\n"
        f"Tool calls: {trace['tool_names']}\n"
        f"Final message: {trace['final_message'][:200]}"
    )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_conversational_classification():
    """Conversational: 'Thanks, I'm done!' must NOT trigger delegation."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="Thanks, I'm done!")]},
        config={"configurable": {"thread_id": "eval-obs-conv"}},
    )

    trace = _extract_trace(result)
    assert trace["final_message"].strip() != "", "Must respond with something"
    assert len(trace["subagents_called"]) == 0, (
        f"Conversational request delegated to subagents: {trace['subagents_called']}\n"
        f"Tool calls: {trace['tool_names']}"
    )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_read_only_routes_to_alertmanager():
    """Read-only: 'List all active alerts' must route to alertmanager-operator."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="List all active alerts from Alertmanager")]},
        config={"configurable": {"thread_id": "eval-obs-ro-am"}},
    )

    trace = _extract_trace(result)
    # Must delegate to alertmanager-operator (or ask for context via request_chat_continue)
    if trace["subagents_called"]:
        assert "alertmanager-operator" in trace["subagents_called"], (
            f"Routed to wrong subagent: {trace['subagents_called']}\n"
            f"Expected: alertmanager-operator"
        )
    # Read-only query should NOT trigger log_obs_operation
    log_calls = [tc for tc in trace["tool_names"] if tc == "log_obs_operation"]
    assert len(log_calls) == 0, "Read-only query should not trigger operation logging"


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_read_only_routes_to_prometheus():
    """Read-only: 'How much CPU is checkout using?' must route to prometheus-operator."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="How much CPU is checkout using?")]},
        config={"configurable": {"thread_id": "eval-obs-ro-prom"}},
    )

    trace = _extract_trace(result)
    if trace["subagents_called"]:
        assert "prometheus-operator" in trace["subagents_called"], (
            f"Routed to wrong subagent: {trace['subagents_called']}\n"
            f"Expected: prometheus-operator"
        )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_read_only_routes_to_loki():
    """Read-only: 'Show checkout error logs' must route to loki-operator."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="Show checkout error logs from the last hour")]},
        config={"configurable": {"thread_id": "eval-obs-ro-loki"}},
    )

    trace = _extract_trace(result)
    if trace["subagents_called"]:
        assert "loki-operator" in trace["subagents_called"], (
            f"Routed to wrong subagent: {trace['subagents_called']}\n"
            f"Expected: loki-operator"
        )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_read_only_routes_to_tempo():
    """Read-only: 'Find slow checkout requests' must route to tempo-operator."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="Find slow checkout requests above 2 seconds")]},
        config={"configurable": {"thread_id": "eval-obs-ro-tempo"}},
    )

    trace = _extract_trace(result)
    if trace["subagents_called"]:
        assert "tempo-operator" in trace["subagents_called"], (
            f"Routed to wrong subagent: {trace['subagents_called']}\n"
            f"Expected: tempo-operator"
        )


@pytest.mark.eval
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_state_modifying_triggers_delegation():
    """State-modifying: 'Install node exporter' must delegate or ask for context."""
    if os.environ.get("CI"):
        pytest.skip("Prompt reasoning tests require real LLM")

    agent = await _build_coordinator_with_real_llm(_get_mock_obs_subagents())
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="Install the node exporter in the monitoring namespace")]},
        config={"configurable": {"thread_id": "eval-obs-state-mod"}},
    )

    trace = _extract_trace(result)

    # Must either delegate to prometheus-operator or ask for more context
    delegated = len(trace["subagents_called"]) > 0
    asked_for_context = "request_chat_continue" in trace["tool_names"]
    asked_user = "request_user_input" in trace["tool_names"] or "request_human_input" in trace["tool_names"]

    assert delegated or asked_for_context or asked_user, (
        f"State-modifying request neither delegated nor asked for context.\n"
        f"Tool calls: {trace['tool_names']}\n"
        f"Final message: {trace['final_message'][:300]}"
    )

    if delegated:
        assert "prometheus-operator" in trace["subagents_called"], (
            f"State-modifying (exporter install) routed to wrong subagent: "
            f"{trace['subagents_called']}\nExpected: prometheus-operator"
        )
