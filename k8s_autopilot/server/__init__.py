"""K8s Autopilot Server package.

Provides the A2A server, server config, server graph factory, executor,
and thread handler.
"""

from __future__ import annotations

from k8s_autopilot.server._server_config import SERVER_ENV_PREFIX, ServerConfig
from k8s_autopilot.server.app import create_app, main
from k8s_autopilot.server.executor import A2AAutoPilotExecutor
from k8s_autopilot.server.server import (
    get_server_url,
    run_server,
    wait_for_server_health,
)
from k8s_autopilot.server.server_graph import make_graph
from k8s_autopilot.server.thread_handler import A2AThreadHandler

__all__ = [
    "A2AAutoPilotExecutor",
    "A2AThreadHandler",
    "SERVER_ENV_PREFIX",
    "ServerConfig",
    "create_app",
    "get_server_url",
    "main",
    "make_graph",
    "run_server",
    "wait_for_server_health",
]
