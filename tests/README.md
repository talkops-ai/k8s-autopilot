# k8s-autopilot Testing Architecture

This document defines the testing strategy for k8s-autopilot. Because we operate a multi-agent system managing live Kubernetes infrastructure via LangGraph, traditional text-based assertions are insufficient. We treat the system as a state machine and validate its behavior across five distinct layers.

## The 5-Layer Strategy

We strictly separate fast, deterministic code paths from slow, stochastic LLM executions.

1. **Unit tests**: Pure functions, routers, and policies (Fake models, in-memory state).
2. **Integration tests**: LangGraph wiring, checkpoints, and interrupts.
3. **Agent Evals**: Trajectory quality and tool selection (Real LLM, mocked MCP tools).
4. **Sandbox tests**: Real cluster mutations (Ephemeral vCluster/k3d).
5. **HITL/A2UI tests**: Human-in-the-loop resume flows.

---

## Layer 1: Unit Testing

Unit tests must execute in milliseconds without network calls or token costs. We use `FakeMessagesListChatModel` to guarantee deterministic LLM outputs. Assertions must target graph state updates and schema validation, not the generated prose.

### Example: Testing the Safety Policy

```python
from k8s_autopilot.schemas.supervisor import SupervisorState
from k8s_autopilot.policies.safety import evaluate_safety

def test_prod_delete_requires_approval():
    state = SupervisorState(
        user_request="Delete namespace payments in prod",
        normalized_intent="delete_namespace",
        target_environment="prod",
        selected_agent="kubernetes_agent",
        tool_history=[],
    )

    result = evaluate_safety(state)
    
    assert result.allowed is False
    assert result.requires_human_approval is True
```

### Example: Node-Level Testing with Fake LLMs

```python
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from k8s_autopilot.agents.helm.nodes.plan_release import plan_release_node
from k8s_autopilot.schemas.helm import HelmAgentState

def test_plan_release_node_returns_structured_plan():
    fake_model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"release_name":"payments","namespace":"payments","chart":"./charts/app","values_files":["values-prod.yaml"]}'
            )
        ]
    )

    state = HelmAgentState(
        user_request="Deploy payment-service to prod",
        target_environment="prod",
        release_plan=None,
    )

    updated = plan_release_node(state=state, model=fake_model)

    assert updated.release_plan.release_name == "payments"
    assert updated.release_plan.namespace == "payments"
```

---

## Layer 2: Integration Testing

Integration tests validate the graph compilation (`build_supervisor_graph()`). We run the real LangGraph runtime and persistence layer but stub out the destructive tools. This ensures checkpoints, subgraph routing, and edge conditions work as intended.

### Example: Graph Routing & Interrupt Validation

```python
import pytest
from k8s_autopilot.graphs.supervisor import build_supervisor_graph
from k8s_autopilot.schemas.supervisor import SupervisorState

@pytest.mark.integration
async def test_supervisor_to_helm_flow(fake_chat_model, checkpoint_store, fake_helm_tool):
    graph = build_supervisor_graph(
        model=fake_chat_model,
        checkpoint_store=checkpoint_store,
        helm_tool=fake_helm_tool,
        interrupts=["before_apply"],
    )

    state = SupervisorState(
        user_request="Deploy payment-service to prod",
        normalized_intent="deploy_application",
        target_environment="prod",
    )

    config = {"configurable": {"thread_id": "it-001"}}
    await graph.ainvoke(state, config=config)
    snapshot = graph.get_state(config)

    assert snapshot.values["selected_agent"] == "helm_agent"
    assert snapshot.next == ("before_apply",)
```

---

## Layer 3: Agent Evals (Trajectories)

Evals use real LLMs (e.g., GPT-4o, Claude 3.5 Sonnet) to measure reasoning and tool selection. To prevent actual infrastructure destruction, we run the LLM against the `SubagentEvalHarness`. This harness intercepts MCP tool calls, records the exact parameters generated, and returns a scripted response.

### Example: Argo Rollouts Eval

```python
import os
import pytest
from tests.evals.subagent_eval_harness import SubagentEvalHarness

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

@pytest.fixture(scope="module")
def harness():
    return SubagentEvalHarness.for_argo_rollouts()

@pytest.mark.timeout(90)
async def test_migrate_deployment(harness):
    if os.environ.get("CI"):
        pytest.skip("Evals require real LLM credentials.")

    trace = await harness.run(
        "[STATE-MODIFYING] Migrate the checkout deployment to a rollout. "
        "Use canary strategy. Apply the changes. Plan approved."
    )

    converted = "convert_deployment_to_rollout" in trace.tool_names
    asked_human = trace.hitl_triggered

    assert converted or asked_human, (
        f"Failed to select migration tool. Selected: {trace.tool_names}"
    )
```

### Dataset-Driven Deep Agent Evals

For the higher-level coordinators (App Operator, Observability Operator), we use YAML-driven datasets rather than writing individual Python tests. Evaluators (both rule-based and LLM-judges) score the `trace` against the expectations.

```yaml
# tests/evals/dataset/deploy_service.yaml
id: deploy_service
user_request: Deploy payment-service to prod using Helm and ArgoCD.
context:
  environment: prod
expectations:
  must_use_agents:
    - helm_agent
    - argocd_agent
  must_call_tools:
    - helm_template
    - argocd_sync
```

---

## Layer 4: Ephemeral Sandbox

These tests run exclusively in CI for release candidates. We provision an isolated vCluster or k3d cluster, inject the real execution tools, and assert on the final Kubernetes resource states using `kubectl` or `kubernetes_asyncio`. 

If a sandbox test fails, it indicates an API mismatch or Helm chart rendering bug that the LLM could not foresee.

---

## Layer 5: HITL / A2UI Testing

Human-in-the-loop workflows are central to k8s-autopilot. We test approvals by asserting the graph suspends at `GraphInterrupt` and properly processes a `Command(resume=...)` payload.

### Example: Testing the Approval Flow

```python
import pytest
from langgraph.types import Command

@pytest.mark.integration
async def test_hitl_approval_resume_flow(build_hitl_graph):
    graph = build_hitl_graph()
    config = {"configurable": {"thread_id": "hitl-001"}}
    
    # 1. Execute until interrupt
    await graph.ainvoke({"user_request": "Apply prod deployment"}, config=config)
    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_approval_node",)

    # 2. Resume execution with simulated human approval
    resume_command = Command(resume={"approved": True, "feedback": "LGTM"})
    final_result = await graph.ainvoke(resume_command, config=config)
    
    assert final_result["approval_state"] == "APPROVED"
```

---

## Test Isolation & CI Pipeline

Use `pytest` markers to strictly isolate environments. Never leak LLM token costs into local, fast-iteration development loops.

```ini
[pytest]
markers =
    unit: Fast, deterministic tests with Fake models. (Pre-commit gate)
    integration: Graph compilation and checkpoints. (PR gate)
    eval: Real LLMs, mocked MCPs. (Merge/Nightly gate)
    sandbox: Ephemeral vCluster deployment. (Release gate)
```

To run a specific pipeline stage locally:
```bash
uv run pytest -m unit
uv run pytest -m integration
uv run pytest tests/evals/app_operator/ -m eval
```
