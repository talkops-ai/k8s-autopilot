"""
Eval runner for Helm Operator scenarios.

Loads YAML scenario files from the dataset directory and runs them against
the HelmOperatorCoordinator.  Provides two execution modes:

- ``run_helm_operator_scenario(scenario)``:
  Runs a scenario against the real coordinator with real LLM.
  Requires a live API key.

- ``run_helm_operator_scenario(scenario, fake_model=<model>)``:
  Runs with a patched coordinator model — useful for CI smoke-tests that
  verify the evaluator pipeline without making real LLM calls.

Design improvements over the original runner.py:
- Subagents are MOCKED — prevents hitting real Helm/GitHub MCP servers.
- Scenario context is injected as a SystemMessage so the coordinator
  doesn't ask for basic details mid-trace.
- asyncio.wait_for(30s) prevents hangs on HITL gates.
- Thinking-mode (Gemini) content extraction filters out 'thinking' parts.
- HITL vs chat_paused distinction in the trace.
- GraphInterrupt captured as partial trace.

Scenarios with ``skip_agent_run: true`` are designed for multi-agent or
supervisor-level evaluation (not the helm-operator in isolation).
"""

import asyncio
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


# Subagent names used by Helm Operator
HELM_OPERATOR_SUBAGENT_NAMES = {
    "helm-planner",
    "helm-generator",
    "helm-validator",
    "helm-updater",
    "helm-operation",
    "helm-skill-builder",
    "github-agent",
    # planner subgraph internals
    "requirements_analyser",
    "architecture_planner",
}

# Coordinator-level tools meaningful for evals
HELM_COORDINATOR_TOOLS = {
    "write_todos",
    "log_helm_operation",
    "request_chat_continue",
    "request_user_input",
    "request_human_input",
    "sync_workspace",
}

# Tools that indicate HITL approval gate fired
_HITL_TOOLS = {"request_user_input", "request_human_input"}

# Tools that indicate graceful conversational pause (not HITL)
_CHAT_PAUSE_TOOLS = {"request_chat_continue"}

# Per-scenario timeout — prevents blocking on HITL or real MCP
_SCENARIO_TIMEOUT_SECONDS = 30


def load_dataset() -> list:
    """Load all YAML scenario files from the helm operator dataset directory."""
    dataset_dir = Path(__file__).parent / "dataset"
    scenarios = []
    if dataset_dir.exists():
        for f in sorted(dataset_dir.glob("*.yaml")):
            with open(f) as file:
                scenarios.append(yaml.safe_load(file))
    return scenarios


def load_helm_dataset() -> list:
    """Alias for load_dataset() — preferred name for Helm-specific imports."""
    return load_dataset()


def _build_mock_helm_subagent_specs() -> list:
    """
    Return mocked subagent specs that respond with scripted success messages.

    CRITICAL: Eval runs must mock subagents to prevent hitting real Helm or
    GitHub MCP servers.  The coordinator's routing decision is what we evaluate
    — not whether the Kubernetes cluster is reachable.
    """
    from langchain_core.messages import AIMessage

    class _MockHelmSubagentRunnable:
        def __init__(self, name: str, response: str):
            self.name = name
            self.response = response

        async def ainvoke(self, state, config=None, **kwargs):
            new_state = dict(state)
            messages = list(new_state.get("messages", []))
            messages.append(AIMessage(content=f"[MOCK] {self.name}: {self.response}"))
            new_state["messages"] = messages
            return new_state

        def with_config(self, config=None):
            return self

    return [
        {
            "name": "helm-planner",
            "description": "Plans Helm chart architecture and requirements.",
            "runnable": _MockHelmSubagentRunnable(
                "helm-planner",
                "Requirements analysed. Architecture planned. Skills written for nginx.",
            ),
        },
        {
            "name": "helm-generator",
            "description": "Generates Helm chart files.",
            "runnable": _MockHelmSubagentRunnable(
                "helm-generator",
                "Chart files generated at /workspace/helm-charts/nginx/.",
            ),
        },
        {
            "name": "helm-validator",
            "description": "Validates Helm charts via helm lint.",
            "runnable": _MockHelmSubagentRunnable(
                "helm-validator",
                "VALID: helm lint and helm template passed.",
            ),
        },
        {
            "name": "helm-updater",
            "description": "Fetches and patches existing Helm charts.",
            "runnable": _MockHelmSubagentRunnable(
                "helm-updater",
                "Chart updated successfully.",
            ),
        },
        {
            "name": "helm-skill-builder",
            "description": "Creates skill directories for new app types.",
            "runnable": _MockHelmSubagentRunnable(
                "helm-skill-builder",
                "Skill directory created.",
            ),
        },
        {
            "name": "helm-operation",
            "description": "Performs live Helm operations on clusters.",
            "runnable": _MockHelmSubagentRunnable(
                "helm-operation",
                "Operation completed. Release status: deployed.",
            ),
        },
        {
            "name": "github-agent",
            "description": "Commits chart files to GitHub.",
            "runnable": _MockHelmSubagentRunnable(
                "github-agent",
                "Chart committed to main branch.",
            ),
        },
    ]


async def run_helm_operator_scenario(
    scenario: Dict[str, Any],
    fake_model=None,
) -> Dict[str, Any]:
    """
    Run a single eval scenario through the HelmOperatorCoordinator.

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

    from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator
    from k8s_autopilot.config.config import Config
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

    config = Config()

    if fake_model is not None:
        from unittest.mock import patch
        ctx = patch("k8s_autopilot.utils.llm.create_model", return_value=fake_model)
        ctx.__enter__()

    coordinator = HelmOperatorCoordinator(config=config)

    # CRITICAL: Always use mocked subagents in eval runs to prevent hitting
    # real Helm or GitHub MCP servers.
    mock_specs = _build_mock_helm_subagent_specs()
    async def _mock_get_subagent_specs():
        return mock_specs
    coordinator.get_subagent_specs = _mock_get_subagent_specs

    agent = await coordinator.build_agent()

    if fake_model is not None:
        ctx.__exit__(None, None, None)

    run_config = {"configurable": {"thread_id": f"eval-helm-{scenario['id']}"}}

    # Inject scenario context as SystemMessage so the coordinator doesn't
    # ask the user for basic details mid-trace.
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
        # Agent stuck — likely waiting on request_chat_continue or real MCP.
        # Capture whatever state was recorded before the timeout.
        try:
            state = agent.get_state(run_config)
            result = state.values if hasattr(state, "values") else {}
        except Exception:
            result = {}
        chat_paused = True
    except Exception as e:
        e_name = type(e).__name__
        if "GraphInterrupt" in e_name or "interrupt" in e_name.lower():
            # HITL gate fired — capture state up to the interrupt
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
                # Gemini thinking-mode: filter out 'thinking' parts, keep 'text' only
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
                    if name in HELM_OPERATOR_SUBAGENT_NAMES:
                        trace["subagents_called"].append(name)
                    # Detect chat pause within the trace itself (before timeout)
                    if name in _CHAT_PAUSE_TOOLS:
                        chat_paused = True
                        trace["chat_paused"] = True

    return trace
