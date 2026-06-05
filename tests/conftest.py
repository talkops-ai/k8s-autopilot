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

@pytest.fixture
def mock_config():
    """
    Minimal Config mock that does not load from disk or env.

    MCP_SERVERS is explicitly set to empty so no stdio MCP processes are
    launched during integration tests.  The LLM config points to a fast model
    but is usually patched further by individual test fixtures.
    """
    config = MagicMock()
    config.get_llm_config.return_value = {"model": "google_genai:gemini-3.1-flash-lite-preview", "temperature": 0}
    config.get_llm_deepagent_config.return_value = {"model": "google_genai:gemini-3.1-flash-lite-preview", "temperature": 0}
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
