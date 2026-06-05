"""
Eval runner for App Operator scenarios.

Mirrors app_operator/runner.py but targets ObservabilityCoordinator
and extracts the three domain subagents (argocd-onboarder,
argo-rollouts-onboarder, traefik-edge-router) from the trajectory.

Design decisions:
- Subagents are MOCKED in eval runs — they return a scripted success message
  so the coordinator's routing decision can be evaluated in isolation without
  hitting real MCP servers (ArgoCD, Rollouts, Traefik).
- Scenario context is injected as a SystemMessage so the coordinator has
  enough information to proceed without calling request_chat_continue for
  missing details.
- request_chat_continue is treated as a graceful stopping point — the trace
  is captured up to that point and evaluated.
- GraphInterrupt (HITL gate) is also captured as a partial trace.

Usage::

    # In CI with fake model (no API key needed):
    from tests.evals.app_operator.runner import run_app_operator_scenario
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    fake = FakeMessagesListChatModel(responses=[AIMessage(content="outside my scope")])
    trace = await run_app_operator_scenario(scenario, fake_model=fake)

    # In eval runs with real LLM:
    trace = await run_app_operator_scenario(scenario)
"""

import asyncio
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


# Subagent names used by App Operator — used to detect delegation in trajectory
APP_OPERATOR_SUBAGENT_NAMES = {
    "argocd-onboarder",
    "argo-rollouts-onboarder",
    "traefik-edge-router",
}

# Coordinator-level tools whose presence in the trace is meaningful for evals
APP_OPERATOR_COORDINATOR_TOOLS = {
    "write_todos",
    "log_app_operation",
    "request_chat_continue",
    "request_user_input",
    "request_human_input",
}

# Tools that indicate the agent paused the graph for HITL approval
_HITL_TOOLS = {"request_user_input", "request_human_input"}

# Tools that indicate the agent paused for conversational continuation (not HITL)
_CHAT_PAUSE_TOOLS = {"request_chat_continue"}

# Per-scenario timeout — prevents blocking on request_user_input (HITL) or real MCP.
# HITL fires at ~15-25s; 30s gives enough headroom while keeping CI fast.
_SCENARIO_TIMEOUT_SECONDS = 30


def load_app_operator_dataset() -> list:
    """Load all YAML scenario files from the app_operator dataset directory."""
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
    run, they hit actual MCP servers (ArgoCD, Rollouts, Traefik) which will
    fail in a dev machine (no real cluster) and pollute the eval with
    infrastructure errors unrelated to coordinator routing quality.
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
            "name": "argocd-onboarder",
            "description": "Manages ArgoCD application onboarding and sync operations.",
            "runnable": _MockSubagentRunnable("argocd-onboarder"),
        },
        {
            "name": "argo-rollouts-onboarder",
            "description": "Manages Argo Rollouts canary/blue-green lifecycle.",
            "runnable": _MockSubagentRunnable("argo-rollouts-onboarder"),
        },
        {
            "name": "traefik-edge-router",
            "description": "Manages Traefik IngressRoute and traffic splitting.",
            "runnable": _MockSubagentRunnable("traefik-edge-router"),
        },
    ]


async def run_app_operator_scenario(
    scenario: Dict[str, Any],
    fake_model=None,
) -> Dict[str, Any]:
    """
    Run a single eval scenario through the AppOperatorCoordinator.

    Args:
        scenario: Parsed YAML scenario dict (id, user_request, expectations, context …).
        fake_model: Optional fake model to patch ``create_model`` with.
                    When provided, no real LLM call is made — useful in CI.

    Returns:
        Trace dict: {subagents_called, tool_calls, final_message, messages,
                     chat_paused, hitl_triggered}.
    """
    # Scenarios marked skip_agent_run are multi-agent / supervisor-level tests
    if scenario.get("skip_agent_run"):
        return {
            "subagents_called": [],
            "tool_calls": [],
            "final_message": "",
            "messages": [],
            "skipped": True,
            "skip_reason": "skip_agent_run=true in scenario",
        }

    from k8s_autopilot.core.agents.app_operator.coordinator import AppOperatorCoordinator
    from k8s_autopilot.config.config import Config
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

    config = Config()

    if fake_model is not None:
        from unittest.mock import patch
        ctx = patch("k8s_autopilot.utils.llm.create_model", return_value=fake_model)
        ctx.__enter__()

    coordinator = AppOperatorCoordinator(config=config)

    # CRITICAL: Always use mocked subagents in eval runs to prevent hitting
    # real MCP servers. The coordinator's routing decision is what we're
    # evaluating — not whether ArgoCD is reachable.
    mock_specs = _build_mock_subagent_specs()
    async def _mock_get_subagent_specs():
        return mock_specs
    coordinator.get_subagent_specs = _mock_get_subagent_specs

    agent = await coordinator.build_agent()

    if fake_model is not None:
        ctx.__exit__(None, None, None)

    run_config = {"configurable": {"thread_id": f"eval-app-op-{scenario['id']}"}}

    # Build initial messages — inject scenario context as SystemMessage so the
    # coordinator doesn't call request_chat_continue asking for basic details.
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
        # Agent is stuck — likely waiting on request_chat_continue or real MCP
        # Capture whatever state was recorded before the timeout
        try:
            state = agent.get_state(run_config)
            result = state.values if hasattr(state, "values") else {}
        except Exception:
            result = {}
        chat_paused = True
    except Exception as e:
        e_name = type(e).__name__
        if "GraphInterrupt" in e_name or "interrupt" in e_name.lower():
            # HITL gate fired — capture state up to interrupt
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
                # Gemini thinking-mode: content is a list of dicts like
                # [{'type': 'thinking', 'thinking': '...'}, {'type': 'text', 'text': '...'}]
                # Extract only the 'text' parts for the final message
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
                    if name in APP_OPERATOR_SUBAGENT_NAMES:
                        trace["subagents_called"].append(name)
                    # Detect chat pause within the trace itself (before timeout)
                    if name in _CHAT_PAUSE_TOOLS:
                        chat_paused = True
                        trace["chat_paused"] = True

    return trace
