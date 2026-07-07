"""Generic messaging integration framework.

Provides abstract base classes and shared utilities for integrating
LangGraph agents with messaging platforms (Slack, Teams, Discord, etc.).
"""

from k8s_autopilot.core.integration.base import (
    IncomingMessage,
    InteractionPayload,
    MessagingIntegration,
)
from k8s_autopilot.core.integration.stream_bridge import (
    GraphStreamBridge,
    StreamSink,
)
from k8s_autopilot.core.integration.thread_store import ThreadStore
from k8s_autopilot.core.integration.identity import IdentityMapper, UserIdentity

__all__ = [
    "IncomingMessage",
    "InteractionPayload",
    "MessagingIntegration",
    "GraphStreamBridge",
    "StreamSink",
    "ThreadStore",
    "IdentityMapper",
    "UserIdentity",
]
