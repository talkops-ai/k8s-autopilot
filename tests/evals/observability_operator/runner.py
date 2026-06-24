"""
Eval runner for Observability Operator scenarios.

Mirrors runner_app_operator.py but targets ObservabilityCoordinator
and extracts the five domain subagents (prometheus-operator,
alertmanager-operator, opentelemetry-operator, loki-operator,
tempo-operator) from the trajectory.

Design decisions:
- Subagents are MOCKED in eval runs — they return a scripted success message
  so the coordinator's routing decision can be evaluated in isolation without
  hitting real MCP servers (Prometheus, Alertmanager, OTel, Loki, Tempo).
- Scenario context is injected as a SystemMessage so the coordinator has
  enough information to proceed without calling request_chat_continue for
  missing details.
- request_chat_continue is treated as a graceful stopping point — the trace
  is captured up to that point and evaluated.
- GraphInterrupt (HITL gate) is also captured as a partial trace.
"""

import asyncio
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


# Subagent names used by Observability Operator — used to detect delegation in trajectory
OBSERVABILITY_SUBAGENT_NAMES = {
    "prometheus-operator",
    "alertmanager-operator",
    "opentelemetry-operator",
    "loki-operator",
    "tempo-operator",
}

# Coordinator-level tools whose presence in the trace is meaningful for evals
OBSERVABILITY_COORDINATOR_TOOLS = {
    "log_obs_operation",
    "request_chat_continue",
    "request_user_input",
    "request_human_input",
}

# Tools that indicate the agent paused the graph for HITL approval
_HITL_TOOLS = {"request_user_input", "request_human_input"}

# Tools that indicate the agent paused for conversational continuation (not HITL)
_CHAT_PAUSE_TOOLS = {"request_chat_continue"}

# Per-scenario timeout
_SCENARIO_TIMEOUT_SECONDS = 30


def load_observability_dataset() -> list:
    """Load all YAML scenario files from the observability_operator dataset directory."""
    dataset_dir = Path(__file__).parent / "dataset"
    scenarios = []
    if dataset_dir.exists():
        for f in sorted(dataset_dir.glob("*.yaml")):
            with open(f) as file:
                scenarios.append(yaml.safe_load(file))
    return scenarios


def _build_mock_subagent_specs() -> list:
    """
    Return mocked subagent specs that respond with a scripted success message.

    CRITICAL: In eval runs, subagents MUST be mocked. If the real subagents
    run, they hit actual MCP servers which will fail in a dev machine (no
    real cluster) and pollute the eval with infrastructure errors unrelated
    to coordinator routing quality.
    """
    from langchain_core.messages import AIMessage

    class _MockSubagentRunnable:
        def __init__(self, name: str):
            self.name = name

        async def ainvoke(self, state, config=None, **kwargs):
            new_state = dict(state)
            messages = list(new_state.get("messages", []))
            messages.append(AIMessage(
                content=f"[MOCK] {self.name} completed successfully. "
                        f"Operation acknowledged."
            ))
            new_state["messages"] = messages
            return new_state

        def with_config(self, config=None):
            return self

    return [
        {
            "name": "prometheus-operator",
            "description": "Manages Prometheus monitoring and metric operations.",
            "runnable": _MockSubagentRunnable("prometheus-operator"),
        },
        {
            "name": "alertmanager-operator",
            "description": "Manages Alertmanager alerting operations.",
            "runnable": _MockSubagentRunnable("alertmanager-operator"),
        },
        {
            "name": "opentelemetry-operator",
            "description": "Manages OpenTelemetry pipeline operations.",
            "runnable": _MockSubagentRunnable("opentelemetry-operator"),
        },
        {
            "name": "loki-operator",
            "description": "Manages Grafana Loki log observability (read-only).",
            "runnable": _MockSubagentRunnable("loki-operator"),
        },
        {
            "name": "tempo-operator",
            "description": "Manages Grafana Tempo distributed tracing.",
            "runnable": _MockSubagentRunnable("tempo-operator"),
        },
    ]


async def run_observability_scenario(
    scenario: Dict[str, Any],
    fake_model=None,
) -> Dict[str, Any]:
    """
    Run a single eval scenario through the ObservabilityCoordinator.

    Args:
        scenario: Parsed YAML scenario dict (id, user_request, expectations, context …).
        fake_model: Optional fake model to patch ``create_model`` with.
                    When provided, no real LLM call is made — useful in CI.

    Returns:
        Trace dict: {subagents_called, tool_calls, final_message, messages,
                     chat_paused, hitl_triggered}.
    """
    if scenario.get("skip_agent_run"):
        return {
            "subagents_called": [],
            "tool_calls": [],
            "final_message": "",
            "messages": [],
            "skipped": True,
            "skip_reason": "skip_agent_run=true in scenario",
        }

    from k8s_autopilot.core.agents.observability.coordinator import ObservabilityCoordinator
    from k8s_autopilot.config.config import Config
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

    config = Config()

    if fake_model is not None:
        from unittest.mock import patch
        ctx = patch("k8s_autopilot.utils.llm.create_model", return_value=fake_model)
        ctx.__enter__()

    coordinator = ObservabilityCoordinator(config=config)

    # CRITICAL: Always use mocked subagents in eval runs
    mock_specs = _build_mock_subagent_specs()
    async def _mock_get_subagent_specs():
        return mock_specs
    coordinator.get_subagent_specs = _mock_get_subagent_specs

    agent = await coordinator.build_agent()

    if fake_model is not None:
        ctx.__exit__(None, None, None)

    run_config = {"configurable": {"thread_id": f"eval-obs-{scenario['id']}"}}

    # Build initial messages — inject scenario context as SystemMessage
    initial_messages = []
    context = scenario.get("context", {})
    if context:
        ctx_lines = "\n".join(f"- {k}: {v}" for k, v in context.items())
        initial_messages.append(SystemMessage(
            content=(
                "## Eval Context (pre-supplied — do NOT ask the user for these details)\n"
                f"{ctx_lines}\n\n"
                "Use these details directly when performing the requested operation."
            )
        ))
    initial_messages.append(HumanMessage(content=scenario["user_request"]))

    initial_state = {"messages": initial_messages}

    result = None
    chat_paused = False
    hitl_triggered = False

    try:
        result = await asyncio.wait_for(
            agent.ainvoke(initial_state, config=run_config),
            timeout=_SCENARIO_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            state = agent.get_state(run_config)
            result = state.values if hasattr(state, "values") else {}
        except Exception:
            result = {}
        chat_paused = True
    except Exception as e:
        e_name = type(e).__name__
        if "GraphInterrupt" in e_name or "interrupt" in e_name.lower():
            hitl_triggered = True
            try:
                state = agent.get_state(run_config)
                result = state.values if hasattr(state, "values") else {}
            except Exception:
                result = {}
        else:
            raise

    messages = (result or {}).get("messages", [])

    trace: Dict[str, Any] = {
        "subagents_called": [],
        "tool_calls": [],
        "final_message": "",
        "messages": [],
        "chat_paused": chat_paused,
        "hitl_triggered": hitl_triggered,
    }

    for m in messages:
        try:
            trace["messages"].append(m.model_dump() if hasattr(m, "model_dump") else dict(m))
        except Exception:
            trace["messages"].append({"content": str(getattr(m, "content", m))})

        if isinstance(m, AIMessage):
            content = m.content
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
            trace["final_message"] = str(content or "")

            if hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc["name"]
                    trace["tool_calls"].append(tc)
                    if name in OBSERVABILITY_SUBAGENT_NAMES:
                        trace["subagents_called"].append(name)
                    if name in _CHAT_PAUSE_TOOLS:
                        chat_paused = True
                        trace["chat_paused"] = True

    return trace
