"""Slack integration for k8s-autopilot.

Provides a standalone Starlette server that wraps Slack Bolt's
``AsyncApp`` and bridges Slack events to LangGraph graph runs.

Quick start::

    from k8s_autopilot.core.integration.slack import (
        create_slack_integration_server,
    )

    app = create_slack_integration_server(config, supervisor_agent)
    uvicorn.run(app, host="0.0.0.0", port=3000)
"""

from k8s_autopilot.core.integration.slack.app import create_slack_integration_server
from k8s_autopilot.core.integration.slack.integration import SlackMessagingIntegration

__all__ = [
    "create_slack_integration_server",
    "SlackMessagingIntegration",
]
