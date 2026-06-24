import pytest
from unittest.mock import MagicMock
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore

from k8s_autopilot.core.agents.observability.coordinator import ObservabilityCoordinator


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.get_llm_config.return_value = {"model": "fake", "temperature": 0}
    config.get_llm_deepagent_config.return_value = {"model": "fake", "temperature": 0}
    return config


@pytest.fixture
def coordinator(mock_config):
    return ObservabilityCoordinator(config=mock_config)


@pytest.fixture
def obs_ops_log_files():
    """Virtual FS with a populated Observability operations journal."""
    return {
        "/memories/observability/operations-log.md": {
            "content": (
                "# Observability Operations Journal\n\n"
                "### INSTALL (2026-06-04 10:00 UTC)\n"
                "- **Target System**: `prometheus`\n"
                "- **Operation**: `exporter`\n"
                "- **Resource**: `node-exporter`\n"
                "- **Namespace**: `monitoring`\n"
            ),
        },
    }


@pytest.fixture
def plan_locked_files():
    """Virtual FS with an active locked plan for observability."""
    return {
        "/plan/active-plan.md": {
            "content": (
                "## Approved Plan\n"
                "- Install node-exporter in monitoring namespace\n"
                "- Create ServiceMonitor for node-exporter\n"
                "- Verify targets appear in Prometheus\n"
            ),
        },
    }
