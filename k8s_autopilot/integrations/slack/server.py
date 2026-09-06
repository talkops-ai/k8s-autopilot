"""Standalone CLI entry point for the Slack integration server.

Run with::

    python -m k8s_autopilot.integrations.slack.server
    # or:
    python -m k8s_autopilot.integrations.slack.server --port 3000

This creates the deep agent via ``create_k8s_autopilot_agent`` and
runs on a separate port with Slack Bolt as the event ingress.
"""

from __future__ import annotations

import logging as _logging
import sys

import click
import uvicorn

from k8s_autopilot.agent import create_k8s_autopilot_agent
from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.integrations.slack.app import (
    create_slack_integration_server,
)
from k8s_autopilot.utils.logger import AgentLogger


class _TracerExceptionFilter(_logging.Filter):
    """Suppress 'No indexed run ID' messages from LangChain's callback manager."""

    def filter(self, record: _logging.LogRecord) -> bool:
        return "No indexed run ID" not in record.getMessage()


_logging.getLogger("langchain_core.callbacks.manager").addFilter(
    _TracerExceptionFilter()
)

logger = AgentLogger("SlackServer")


@click.command()
@click.option(
    "--host",
    default="0.0.0.0",  # noqa: S104
    help="Server host (default: 0.0.0.0)",
)
@click.option(
    "--port",
    type=int,
    default=3000,
    help="Server port (default: 3000)",
)
@click.option(
    "--config-file",
    default=None,
    help="Path to configuration file",
)
def main(host: str, port: int, config_file: str | None) -> None:
    """Start the Slack integration server."""
    try:
        settings = get_settings()

        slack_enabled = getattr(settings, "slack_enabled", False)
        slack_bot_token = getattr(settings, "slack_bot_token", None)
        slack_signing_secret = getattr(settings, "slack_signing_secret", None)

        if not slack_enabled:
            logger.warning(
                "SLACK_ENABLED is False — set SLACK_ENABLED=true to enable",
            )
            sys.exit(1)

        if not slack_bot_token or not slack_signing_secret:
            logger.error(
                "SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET are required",
            )
            sys.exit(1)

        logger.info("Initializing K8s Autopilot deep agent for Slack…")

        agent_graph, backend = create_k8s_autopilot_agent(
            model=settings.model,
            auto_approve=(settings.approval_mode == "yolo"),
        )

        logger.info("Deep agent ready for Slack")

        app = create_slack_integration_server(
            config=settings,
            supervisor_agent=agent_graph,
        )

        logger.info(
            f"Starting Slack integration server on {host}:{port}",
            extra={"host": host, "port": port},
        )

        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=settings.log_level.lower(),
        )

    except Exception as exc:
        logger.error(
            f"Failed to start Slack integration server: {exc}",
            extra={"error": str(exc)},
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
