# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
"""Standalone Starlette server for Slack integration.

This is a **SEPARATE** server from the existing ``server.py`` (A2A).
It can be:

1. **Run standalone**: ``python -m k8s_autopilot.integrations.slack.app``
2. **Mounted via langgraph.json** ``http.app`` when migrating to
   LangGraph Platform (zero code changes needed)
3. **Run as a sidecar container** in Kubernetes

Architecture::

    Starlette
      └─ Mount("/slack", app=AsyncSlackRequestHandler(bolt_app))
      └─ Route("/health", health_handler)

The Slack Bolt ``AsyncApp`` handles all event verification, routing,
and retries via its built-in ASGI adapter.
"""

from __future__ import annotations

import contextlib
from typing import Any

from slack_bolt.adapter.starlette.async_handler import (  # type: ignore[import-not-found]
    AsyncSlackRequestHandler,
)
from slack_bolt.async_app import AsyncApp  # type: ignore[import-not-found]
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from k8s_autopilot.config.settings import Settings, get_settings
from k8s_autopilot.integrations.slack.bolt_handlers import (
    register_bolt_handlers,
)
from k8s_autopilot.integrations.slack.integration import (
    SlackMessagingIntegration,
)
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("SlackIntegrationServer")


def create_slack_integration_server(
    config: Settings | None = None,
    supervisor_agent: Any = None,
) -> Starlette:
    """Create the standalone Starlette server for Slack integration.

    This factory function:

    1. Creates a Slack Bolt ``AsyncApp`` with the configured tokens.
    2. Instantiates :class:`SlackMessagingIntegration` connecting Bolt
       to the LangGraph graph.
    3. Registers all Bolt event / action handlers.
    4. Wraps the Bolt app in a Starlette ``Mount`` with an ASGI adapter.
    5. Returns the Starlette application ready for ``uvicorn.run()``.

    Args:
        config: Application configuration with ``SLACK_*`` keys.
        supervisor_agent: Any LangGraph agent with a ``.graph``
            attribute (e.g. ``k8sAutopilotSupervisorAgent``).

    Returns:
        A Starlette application that handles Slack webhooks.
    """
    # ── Validate required config (degrade gracefully for setup UI) ────
    cfg = config or get_settings()
    bot_token = getattr(cfg, "slack_bot_token", None) or getattr(cfg, "SLACK_BOT_TOKEN", None) or "xoxb-dummy-token"
    signing_secret = (
        getattr(cfg, "slack_signing_secret", None)
        or getattr(cfg, "SLACK_SIGNING_SECRET", None)
        or "dummy-signing-secret"
    )

    # ── Initialize Slack Bolt AsyncApp ────────────────────────────────
    bolt_app = AsyncApp(
        token=bot_token,
        signing_secret=signing_secret,
    )

    # ── Resolve the LangGraph compiled graph ──────────────────────────
    # k8sAutopilotSupervisorAgent stores it as ._graph (private)
    # Other agents may use .graph (public). Fall back to the object itself.
    graph = getattr(supervisor_agent, "_graph", None) or getattr(supervisor_agent, "graph", None) or supervisor_agent

    # ── Create the integration ────────────────────────────────────────
    integration = SlackMessagingIntegration(
        config=config,
        bolt_app=bolt_app,
        graph=graph,
    )

    # ── Register Bolt handlers ────────────────────────────────────────
    register_bolt_handlers(bolt_app, integration)

    # ── Create Bolt ASGI handler ──────────────────────────────────────
    bolt_handler = AsyncSlackRequestHandler(bolt_app)

    # ── Health endpoint ───────────────────────────────────────────────
    async def health(request: Any) -> JSONResponse:
        """Health check endpoint."""
        return JSONResponse(
            {
                "status": "healthy",
                "integration": "slack",
                "bolt_version": "active",
            },
        )

    # ── Info endpoint ─────────────────────────────────────────────────
    async def info(request: Any) -> JSONResponse:
        """Integration info endpoint."""
        return JSONResponse(
            {
                "name": "k8s-autopilot-slack",
                "platform": "slack",
                "capabilities": [
                    "dm_messages",
                    "app_mentions",
                    "assistant_container",
                    "hitl_approval",
                    "streaming",
                    "block_kit",
                ],
            },
        )

    # ── Lifespan ──────────────────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        """Manage Slack integration server lifespan and checkpointer upgrades.

        Args:
            app: Starlette application instance.

        Yields:
            None: Context during server execution.
        """
        logger.info(
            "Slack integration server starting...",
            extra={"bolt_enabled": True},
        )
        # Lazily upgrade supervisor agent checkpointer to AsyncPostgresSaver if available
        if hasattr(supervisor_agent, "_ensure_async_checkpointer"):
            try:
                await supervisor_agent._ensure_async_checkpointer()
                upgraded_graph = (
                    getattr(supervisor_agent, "_graph", None)
                    or getattr(supervisor_agent, "graph", None)
                    or supervisor_agent
                )
                integration.stream_bridge.graph = upgraded_graph
                logger.info("Supervisor checkpointer upgraded in Slack integration")
            except Exception as exc:
                logger.warning(f"Failed to upgrade checkpointer in Slack lifespan: {exc}")

        yield

        # Server cleanup
        logger.info("Slack integration server shutting down")

    def sync_slack_credentials() -> None:
        """Synchronize runtime Slack tokens and secrets with active config."""
        if config.SLACK_BOT_TOKEN:
            bolt_app.client.token = config.SLACK_BOT_TOKEN
            bolt_app._token = config.SLACK_BOT_TOKEN
            if hasattr(integration, "client"):
                integration.client.token = config.SLACK_BOT_TOKEN
        if config.SLACK_SIGNING_SECRET:
            bolt_app._signing_secret = config.SLACK_SIGNING_SECRET
            for mw in getattr(bolt_app, "_async_middleware_list", []):
                if mw.__class__.__name__ == "AsyncRequestVerification":
                    verifier = getattr(mw, "verifier", None)
                    if verifier and hasattr(verifier, "signing_secret"):
                        verifier.signing_secret = config.SLACK_SIGNING_SECRET

    # ── Bolt route handler ─────────────────────────────────────────────
    # AsyncSlackRequestHandler is NOT an ASGI app — it exposes a
    # .handle(request) -> Response method for Starlette requests.
    # We wrap it in a simple route handler for each endpoint.
    async def slack_events(request: Any) -> Any:
        """Forward Slack Events API requests to Bolt."""
        sync_slack_credentials()
        return await bolt_handler.handle(request)

    async def slack_interactions(request: Any) -> Any:
        """Forward Slack Interactivity requests to Bolt."""
        sync_slack_credentials()
        return await bolt_handler.handle(request)

    async def slack_catch_all(request: Any) -> Any:
        """Catch-all for any other /slack/* routes Bolt may need."""
        sync_slack_credentials()
        return await bolt_handler.handle(request)

    # ── Build Starlette app ───────────────────────────────────────────
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/info", info, methods=["GET"]),
            # Slack Bolt endpoints
            Route("/slack/events", slack_events, methods=["POST"]),
            Route("/slack/interactions", slack_interactions, methods=["POST"]),
            Route("/slack/install", slack_catch_all, methods=["GET"]),
            Route("/slack/oauth_redirect", slack_catch_all, methods=["GET"]),
        ],
        lifespan=lifespan,
    )

    app.state.integration = integration

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logger.info(
        "Slack integration server created",
        extra={
            "routes": [
                "/health",
                "/info",
                "/slack/events",
                "/slack/interactions",
            ],
        },
    )

    return app
