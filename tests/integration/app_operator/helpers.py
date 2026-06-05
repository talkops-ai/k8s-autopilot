"""
Shared test helpers for App Operator integration tests.

Import these in test files as:
    from tests.integration.app_operator.helpers import ExhaustingFakeModel, make_argocd_subagent
"""
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class ExhaustingFakeModel(FakeMessagesListChatModel):
    """
    FakeModel that raises RuntimeError when scripted responses run out.

    This is the correct fake model for integration tests: if the agent keeps
    calling the LLM after all scripted responses are consumed, it means the
    agent is stuck in a loop — the error surfaces the bug immediately instead
    of silently cycling.
    """

    def bind_tools(self, tools, **kwargs):
        return self

    def _get_next_response(self):
        if self.i >= len(self.responses):
            raise RuntimeError(
                f"ExhaustingFakeModel: all {len(self.responses)} scripted responses "
                "consumed. The agent made more LLM calls than expected — "
                "check for an infinite loop or add more responses to the fixture."
            )
        response = self.responses[self.i]
        self.i += 1
        return response

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._get_next_response()
        from langchain_core.outputs import ChatGeneration, ChatResult
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class MockSubAgent:
    """
    Synchronous/async mock that mimics a CompiledSubAgent.

    Tracks call count so tests can assert delegation happened exactly once
    (avoiding over-delegation bugs).
    """

    def __init__(self, name: str, response_content: str, extra_state=None):
        self.name = name
        self.response_content = response_content
        self.extra_state = extra_state or {}
        self.calls = 0

    def __contains__(self, key):
        if key == "graph_id":
            return False
        return hasattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def with_config(self, config=None):
        return self

    async def ainvoke(self, state, config=None, **kwargs):
        self.calls += 1
        new_state = dict(state)
        messages = list(new_state.get("messages", []))
        messages.append(AIMessage(content=self.response_content))
        new_state["messages"] = messages
        if self.extra_state:
            new_state.update(self.extra_state)
        return new_state


def make_mock_subagent(name: str, response: str) -> dict:
    """Return a mock subagent dict compatible with coordinator's subagent list."""
    agent = MockSubAgent(name=name, response_content=response)
    return {"name": name, "description": f"mock {name}", "runnable": agent, "_mock": agent}


def make_argocd_subagent(response: str = "Completed ArgoCD operation: app created") -> dict:
    return make_mock_subagent("argocd-onboarder", response)


def make_rollouts_subagent(response: str = "Completed Argo Rollouts operation: rollout updated") -> dict:
    return make_mock_subagent("argo-rollouts-onboarder", response)


def make_traefik_subagent(response: str = "Completed Traefik operation: route created") -> dict:
    return make_mock_subagent("traefik-edge-router", response)
