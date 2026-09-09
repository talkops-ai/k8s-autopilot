import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class DummyCheckpointStore:
    def __init__(self):
        self.state = {}

    def put(self, key, value):
        self.state[key] = value

    def get(self, key, default=None):
        return self.state.get(key, default)


@pytest.fixture(scope="session")
def checkpoint_store():
    return DummyCheckpointStore()


@pytest.fixture(scope="function")
def fake_chat_model_deploy():
    return FakeMessagesListChatModel(
        responses=[
            AIMessage(content='{"release_name":"payments","namespace":"payments","chart":"./charts/payments","values_files":["values-prod.yaml"],"risk":"high"}')
        ]
    )


@pytest.fixture(scope="function")
def fake_chat_model_traffic():
    return FakeMessagesListChatModel(
        responses=[
            AIMessage(content='{"route":"canary","weight":10,"service":"payments","namespace":"payments"}')
        ]
    )


@pytest.fixture(scope="function")
def fake_helm_tool():
    class FakeHelmTool:
        def template(self, chart, values_files=None, namespace=None):
            return {
                "manifest": f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {chart}
  namespace: {namespace or 'default'}
""",
                "chart": chart,
                "values_files": values_files or [],
                "namespace": namespace or "default",
            }

        def diff(self, *args, **kwargs):
            return {"changed": True, "summary": "mock diff"}

    return FakeHelmTool()


@pytest.fixture(scope="function")
def fake_policy_engine():
    class FakePolicyEngine:
        def validate(self, state):
            return {"allowed": True, "requires_approval": state.target_environment == "prod", "violations": []}

    return FakePolicyEngine()

from unittest.mock import MagicMock
from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.memory import MemorySaver

class TestConfig(MagicMock):
    def __getattr__(self, name):
        import os
        if name in (
            "GITHUB_REPO",
            "GITHUB_BRANCH",
            "HELM_WORKSPACE",
            "ORG_NAME",
            "ENVIRONMENT",
            "K8S_CONTEXT",
            "KUBECONFIG",
            "K8S_DEFAULT_NAMESPACE",
            "AGENT_PROJECT_ROOT",
        ):
            val = os.getenv(name)
            if val is not None:
                return val
            defaults = {
                "GITHUB_BRANCH": "main",
                "HELM_WORKSPACE": "./workspace/helm-charts",
                "ORG_NAME": "default_org",
                "ENVIRONMENT": "development",
                "K8S_DEFAULT_NAMESPACE": "default",
            }
            return defaults.get(name)
        return super().__getattr__(name)

@pytest.fixture
def mock_config():
    """
    Minimal Config mock that does not load from disk or env.

    MCP_SERVERS is explicitly set to empty so no stdio MCP processes are
    launched during integration tests.  The LLM config points to a fast model
    but is usually patched further by individual test fixtures.
    """
    config = TestConfig()
    config.get_llm_config.return_value = {"model": "google_genai:gemini-3.1-flash-lite", "temperature": 0}
    config.get_llm_deepagent_config.return_value = {"model": "google_genai:gemini-3.1-flash-lite", "temperature": 0}
    # Return empty MCP config — prevents build_agent() from launching real MCP servers
    _empty_mcp = {"servers": [], "timeout": {"total": 10, "connect": 5}, "default_host": "localhost", "default_transport": "sse"}
    config.get_mcp_config.return_value = _empty_mcp
    config.mcp_config = _empty_mcp
    # config.get() is called by MCPClient for MCP_TIMEOUT_TOTAL — return sensible defaults
    config.get.side_effect = lambda key, default=None: {
        "MCP_TIMEOUT_TOTAL": 10.0,
        "MCP_TIMEOUT_CONNECT": 5.0,
    }.get(key, default)
    return config

@pytest.fixture
def in_memory_store():
    return InMemoryStore()

from typing import Any, cast
from langgraph.graph.state import CompiledStateGraph

original_ainvoke: Any = CompiledStateGraph.ainvoke
original_invoke: Any = CompiledStateGraph.invoke

async def patched_ainvoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
    if config is None:
        config = {"configurable": {"thread_id": "test-thread-id"}}
    elif "configurable" not in config:
        config["configurable"] = {"thread_id": "test-thread-id"}
    elif "thread_id" not in config.get("configurable", {}):
        config["configurable"]["thread_id"] = "test-thread-id"
    return await original_ainvoke(self, input, config, **kwargs)

def patched_invoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
    if config is None:
        config = {"configurable": {"thread_id": "test-thread-id"}}
    elif "configurable" not in config:
        config["configurable"] = {"thread_id": "test-thread-id"}
    elif "thread_id" not in config.get("configurable", {}):
        config["configurable"]["thread_id"] = "test-thread-id"
    return original_invoke(self, input, config, **kwargs)

CompiledStateGraph.ainvoke = cast(Any, patched_ainvoke)
CompiledStateGraph.invoke = cast(Any, patched_invoke)

@pytest.fixture
def memory_saver():
    return MemorySaver()

@pytest.fixture
def empty_helm_files():
    return {}

@pytest.fixture
def workspace_files_nginx():
    """Simulated virtual FS output from helm-generator."""
    return {
        "/workspace/helm-charts/nginx/Chart.yaml": {"content": "apiVersion: v2\nname: nginx"},
        "/workspace/helm-charts/nginx/values.yaml": {"content": "replicaCount: 1"},
        "/workspace/helm-charts/nginx/templates/deployment.yaml": {"content": "---"},
    }

@pytest.fixture
def fake_model_conversational():
    """Model that returns a conversational reply (no tool calls)."""
    return FakeMessagesListChatModel(
        responses=[AIMessage(content="You're welcome! Let me know if you need anything else.")]
    )

@pytest.fixture(autouse=True)
def mock_llm_creator_fallback():
    """
    Autouse fixture that intercepts langchain init_chat_model calls.
    If the requested model is a Google/Gemini model and no GOOGLE_API_KEY / GEMINI_API_KEY
    is set in the environment, it returns a FakeMessagesListChatModel to prevent validation errors.
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain.chat_models import init_chat_model as _original_init
    from unittest.mock import patch
    import os

    def fallback_init(model, **kwargs):
        class BindableFakeModel(FakeMessagesListChatModel):
            thinking_level: str | None = None
            thinking_budget: int | None = None
            reasoning_effort: str | None = None
            use_responses_api: bool = False

            def bind_tools(self, tools, **kwargs):
                return self

        return BindableFakeModel(
            responses=[AIMessage(content="Mocked response")],
            thinking_level=kwargs.get("thinking_level"),
            thinking_budget=kwargs.get("thinking_budget"),
            reasoning_effort=kwargs.get("reasoning_effort"),
            use_responses_api=bool(kwargs.get("use_responses_api", False)),
        )

    with patch("langchain.chat_models.init_chat_model", side_effect=fallback_init):
        yield


@pytest.fixture(autouse=True)
def isolate_test_data_dir(tmp_path_factory, monkeypatch):
    """Ensure tests run against an isolated temporary data dir and NEVER pollute ~/.k8s_autopilot/ or ~/.k8s_autopilot/.env."""
    test_data = tmp_path_factory.mktemp(".k8s_autopilot_data")
    test_state = test_data / ".state"
    test_env = test_data / ".env"

    import k8s_autopilot.config.paths as p

    monkeypatch.setattr(p, "DATA_DIR", test_data)
    monkeypatch.setattr(p, "STATE_DIR", test_state)
    monkeypatch.setattr(p, "GLOBAL_ENV_PATH", test_env)
    monkeypatch.setattr(p, "CONFIG_PATH", test_data / "config.toml")
    monkeypatch.setattr(p, "GLOBAL_MCP_PATH", test_data / ".mcp.json")
    monkeypatch.setattr(p, "CONVERSATION_HISTORY_DIR", test_data / "conversation_history")
    monkeypatch.setattr(p, "PLUGINS_DIR", test_data / "plugins")
    monkeypatch.setattr(p, "SESSIONS_DB_PATH", test_state / "sessions.db")
    monkeypatch.setattr(p, "HISTORY_PATH", test_state / "history.jsonl")
    monkeypatch.setattr(p, "MCP_TRUST_PATH", test_state / "mcp_trust.json")
    monkeypatch.setattr(p, "SKILL_TRUST_PATH", test_state / "skill_trust.json")

    import os

    if not any(k in os.environ for k in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")):
        monkeypatch.setenv("GOOGLE_API_KEY", "mock-test-key")

