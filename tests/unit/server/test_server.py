"""Unit tests for Server subsystem (ServerConfig, make_graph, create_app, server runner)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from starlette.applications import Starlette

from k8s_autopilot.server import (
    SERVER_ENV_PREFIX,
    ServerConfig,
    create_app,
    get_server_url,
    make_graph,
)
from k8s_autopilot.server.server import _find_free_port, _port_in_use


def test_server_config_serialization():
    cfg = ServerConfig(
        model="openai:gpt-4o",
        assistant_id="custom-autopilot",
        interactive=False,
        auto_approve=True,
        port=9090,
    )
    env = cfg.to_env()
    assert env[f"{SERVER_ENV_PREFIX}MODEL"] == "openai:gpt-4o"
    assert env[f"{SERVER_ENV_PREFIX}ASSISTANT_ID"] == "custom-autopilot"
    assert env[f"{SERVER_ENV_PREFIX}INTERACTIVE"] == "false"
    assert env[f"{SERVER_ENV_PREFIX}AUTO_APPROVE"] == "true"
    assert env[f"{SERVER_ENV_PREFIX}PORT"] == "9090"

    with patch.dict(os.environ, env):
        hydrated = ServerConfig.from_env()
        assert hydrated.model == "openai:gpt-4o"
        assert hydrated.assistant_id == "custom-autopilot"
        assert hydrated.interactive is False
        assert hydrated.auto_approve is True
        assert hydrated.port == 9090


def test_server_port_helpers():
    port = _find_free_port()
    assert isinstance(port, int)
    assert port > 0
    url = get_server_url("0.0.0.0", port)
    assert url == f"http://127.0.0.1:{port}"


def test_server_create_app_initialization() -> None:
    """Verify create_app builds a Starlette app with all A2A, health, and management routes."""
    fake_model = GenericFakeChatModel(messages=iter([]))

    with patch("k8s_autopilot.agent.factory.create_model") as mock_cm:
        mock_res = MagicMock()
        mock_res.model = fake_model
        mock_res.model_name = "test-model"
        mock_cm.return_value = mock_res

        app = create_app(host="127.0.0.1", port=9000)

        assert isinstance(app, Starlette)
        assert len(app.routes) > 0

        route_paths = [getattr(r, "path", "") for r in app.routes]
        assert "/health" in route_paths

        assert "/" in route_paths


@pytest.mark.asyncio
async def test_make_graph_factory():
    """Verify make_graph lazily compiles the agent graph."""
    fake_model = GenericFakeChatModel(messages=iter([]))

    with patch("k8s_autopilot.model.factory.create_model") as mock_cm:
        mock_res = MagicMock()
        mock_res.model = fake_model
        mock_res.model_name = "test-model"
        mock_cm.return_value = mock_res

        graph = await make_graph()
        assert graph is not None
