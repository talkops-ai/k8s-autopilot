"""Generic messaging integration framework.

Provides abstract base classes and shared utilities for integrating
LangGraph agents with messaging platforms (Slack, Teams, Discord, etc.).
"""

from k8s_autopilot.integrations.base import (
    IncomingMessage,
    InteractionPayload,
    MessagingIntegration,
)
from k8s_autopilot.integrations.identity import IdentityMapper, UserIdentity
from k8s_autopilot.integrations.stream_bridge import (
    GraphStreamBridge,
    StreamSink,
)
from k8s_autopilot.integrations.thread_store import ThreadStore

__all__ = [
    "GraphStreamBridge",
    "IdentityMapper",
    "IncomingMessage",
    "InteractionPayload",
    "MessagingIntegration",
    "StreamSink",
    "ThreadStore",
    "UserIdentity",
]
