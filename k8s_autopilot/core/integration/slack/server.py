"""Standalone CLI entry point for the Slack integration server.

Run with::

    python -m k8s_autopilot.core.integration.slack.server
    # or:
    python -m k8s_autopilot.core.integration.slack.server --port 3000 --config-file config.yaml

This creates the same supervisor agent as ``server.py`` (A2A) but
runs on a separate port with Slack Bolt as the event ingress.
"""

from __future__ import annotations

import sys

import click
import uvicorn

from k8s_autopilot.config.config import Config
from k8s_autopilot.core.agents import (
    AppOperatorCoordinator,
    HelmOperatorCoordinator,
    K8sOperatorCoordinator,
    ObservabilityCoordinator,
    create_k8sAutopilotSupervisorAgent,
)
from k8s_autopilot.core.integration.slack.app import (
    create_slack_integration_server,
)
from k8s_autopilot.utils.logger import AgentLogger

# ── Suppress LangChainTracer orphaned-run-ID noise ───────────────────────
# When the supervisor streams with subgraphs=True, the auto-injected
# LangChainTracer (from LANGCHAIN_TRACING_V2=true) receives on_*_end
# events from nested deep-agent subgraphs whose on_*_start it never saw.
# This produces harmless but noisy "No indexed run ID" TracerExceptions.
import logging as _logging


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
    """Start the Slack integration server.

    This server runs independently from the A2A server and handles
    all Slack Events API webhooks and Block Kit interactions.
    """
    try:
        # ── Load config ───────────────────────────────────────────────
        config = Config.load_config(config_file) if config_file else Config()

        if not config.SLACK_ENABLED:
            logger.warning(
                "SLACK_ENABLED is False — set SLACK_ENABLED=true to enable",
            )
            sys.exit(1)

        if not config.SLACK_BOT_TOKEN or not config.SLACK_SIGNING_SECRET:
            logger.error(
                "SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET are required",
            )
            sys.exit(1)

        # ── Build supervisor agent (same as server.py) ────────────────
        logger.info("Initializing supervisor agent…")

        supervisor_agent = create_k8sAutopilotSupervisorAgent(
            config=config,
            name="k8sAutopilotSupervisorAgent",
            coordinators=[
                HelmOperatorCoordinator(config=config),
                K8sOperatorCoordinator(config=config),
                AppOperatorCoordinator(config=config),
                ObservabilityCoordinator(config=config),
            ],
        )

        if not supervisor_agent.is_ready():
            msg = "Supervisor agent failed to initialize"
            raise RuntimeError(msg)

        logger.info(
            "Supervisor agent ready",
            extra={
                "agents": supervisor_agent.list_agents(),
            },
        )

        # ── Build Starlette app ───────────────────────────────────────
        app = create_slack_integration_server(
            config=config,
            supervisor_agent=supervisor_agent,
        )

        # ── Run ───────────────────────────────────────────────────────
        logger.info(
            f"Starting Slack integration server on {host}:{port}",
            extra={"host": host, "port": port},
        )

        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=config.log_level.lower(),
        )

    except Exception as exc:
        logger.error(
            f"Failed to start Slack integration server: {exc}",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
