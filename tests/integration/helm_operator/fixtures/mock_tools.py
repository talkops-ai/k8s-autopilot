from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel


class MockSubAgent:
    def __init__(self, name, response_content, extra_state=None):
        self.name = name
        self.response_content = response_content
        self.extra_state = extra_state or {}
        self.calls = 0

    def __contains__(self, key):
        if key == "graph_id": return False
        return hasattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def with_config(self, config=None):
        return self

    async def ainvoke(self, state, config=None, **kwargs):
        self.calls += 1
        new_state = dict(state)
        messages = new_state.get("messages", [])
        messages.append(AIMessage(content=self.response_content))
        new_state["messages"] = messages
        if self.extra_state:
            new_state.update(self.extra_state)
        return new_state


class ExhaustingFakeModel(FakeMessagesListChatModel):
    """
    A FakeMessagesListChatModel that raises RuntimeError when responses are
    exhausted instead of cycling back to the first response.

    This prevents integration tests from hanging in infinite loops when the
    FakeModel's scripted responses are consumed.

    Usage::

        model = ExhaustingFakeModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "helm-planner", "args": {}, "id": "tc1"}]),
            AIMessage(content="Done"),
        ])
    """

    def bind_tools(self, tools, **kwargs):
        return self

    def _get_next_response(self):
        """Override to raise instead of cycling."""
        if self.i >= len(self.responses):
            raise RuntimeError(
                f"ExhaustingFakeModel: all {len(self.responses)} scripted responses "
                f"have been consumed. The agent made more LLM calls than expected. "
                f"Add more responses to the fixture or check for an infinite loop."
            )
        response = self.responses[self.i]
        self.i += 1
        return response

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._get_next_response()
        # Wrap in a proper ChatGeneration
        from langchain_core.outputs import ChatGeneration, ChatResult
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def make_exhausting_coordinator_model(responses):
    """
    Factory: returns an ExhaustingFakeModel with the given scripted responses.

    Args:
        responses: List of AIMessage objects to return in sequence.
                   When exhausted, raises RuntimeError instead of cycling.
    """
    return ExhaustingFakeModel(responses=responses)


def get_fake_planner_subagent():
    agent = MockSubAgent(name="helm-planner", response_content="Planning complete")
    return {"name": "helm-planner", "description": "mock", "runnable": agent}


def get_fake_generator_subagent(files_dict):
    agent = MockSubAgent(
        name="helm-generator",
        response_content="Generated 7 files",
        extra_state={"files": files_dict}
    )
    return {"name": "helm-generator", "description": "mock", "runnable": agent}


def get_fake_validator_valid():
    agent = MockSubAgent(name="helm-validator", response_content="VALID: all checks passed")
    return {"name": "helm-validator", "description": "mock", "runnable": agent}
