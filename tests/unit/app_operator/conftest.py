import pytest
from unittest.mock import MagicMock
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore

from k8s_autopilot.core.agents.app_operator.coordinator import AppOperatorCoordinator


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.get_llm_config.return_value = {"model": "fake", "temperature": 0}
    config.get_llm_deepagent_config.return_value = {"model": "fake", "temperature": 0}
    return config


@pytest.fixture
def coordinator(mock_config):
    return AppOperatorCoordinator(config=mock_config)


@pytest.fixture
def ops_log_files():
    """Virtual FS with a populated App operations journal."""
    return {
        "/memories/app-operator/operations-log.md": {
            "content": (
                "# App Operations Journal\n\n"
                "### CREATE (2026-06-04 10:00 UTC)\n"
                "- **Application**: `frontend`\n"
                "- **Namespace**: `staging`\n"
                "- **Repo URL**: `https://github.com/org/frontend`\n"
            ),
        },
    }


@pytest.fixture
def plan_locked_files():
    """Virtual FS with an active locked plan."""
    return {
        "/plan/active-plan.md": {
            "content": (
                "## Approved Plan\n"
                "- Create ArgoCD Application 'api-service'\n"
                "- Project: payments\n"
                "- Namespace: production\n"
                "- Source: https://github.com/org/api-service\n"
            ),
        },
    }
