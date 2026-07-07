"""Pre-graph operation gate for the Slack integration.

Classifies incoming user messages as ``read`` or ``write`` operations
using an LLM with structured output — the same pattern used by the
supervisor's ``classify_request`` node
(:class:`~k8s_autopilot.core.agents.supervisor_agent.RouterDecision`).

If the operation is ``write`` and the target agent is **not** in the
configured write whitelist, the gate blocks the request with a
friendly redirect message.  Read operations always pass through.

Architecture::

    Slack message
      → SlackOperationGate.classify(text)
          → LLM structured output → SlackOperationClassification
          → whitelist check
      → GateResult(allowed=True/False)

Configuration (``default.py`` / env vars)::

    SLACK_OPERATION_MODE   = "read"        # "read" | "readwrite"
    SLACK_WRITE_WHITELIST  = []            # e.g. ["observability_operator"]

References:
    - LangChain structured output:
      https://docs.langchain.com/oss/python/langchain/structured-output
    - LangGraph routing pattern:
      https://docs.langchain.com/oss/python/langgraph/workflows-agents#routing
    - Supervisor RouterDecision:
      k8s_autopilot/core/agents/supervisor_agent.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("SlackOperationGate")


# ---------------------------------------------------------------------------
# Classification schema — mirrors supervisor's RouterDecision pattern
# ---------------------------------------------------------------------------


class SlackOperationClassification(BaseModel):
    """Structured classification of a Slack user request.

    The LLM produces this via ``model.with_structured_output()``,
    exactly as the supervisor produces :class:`RouterDecision`.
    """

    operation_type: Literal["read", "write"] = Field(
        description=(
            "Whether this request reads/observes cluster state ('read') "
            "or mutates/changes it ('write')."
        ),
    )
    agent: Literal[
        "helm_operator",
        "k8s_operator",
        "app_operator",
        "observability_operator",
        "general",
    ] = Field(
        description="The deep agent domain this request primarily targets.",
    )
    reasoning: str = Field(
        description="Brief explanation of the classification decision.",
    )
    user_message: str = Field(
        description=(
            "A friendly, contextual message to show the user. "
            "For read operations: a brief acknowledgement. "
            "For write operations: explain why this specific request "
            "is not available via Slack yet, what the user asked for, "
            "and suggest alternative read-only actions they can take "
            "via Slack instead. Use standard Markdown formatting "
            "(**bold**, - bullet points on separate lines)."
        ),
    )


# ---------------------------------------------------------------------------
# Classification prompt
# ---------------------------------------------------------------------------

GATE_CLASSIFICATION_PROMPT = """\
<role>
You are a Kubernetes operations classifier for a Slack integration.
Classify each user request as a read or write operation,
identify which agent domain it targets, and generate an appropriate
user-facing message.
Do not perform any actions — only classify and generate the message.
</role>

<operation_rules>
READ operations (observing, inspecting, querying — no cluster state changes):
- List, get, describe, show, check, inspect resources
- View pod logs, events, metrics, alerts, traces
- Query Prometheus, Loki, Tempo, Grafana dashboards
- Check cluster health, node status, resource usage
- Helm list, helm status, helm history, helm get values
- ArgoCD app status, sync status, rollout status
- Describe ingress, services, deployments, configmaps
- Explain errors, diagnose issues, troubleshoot
- "What pods are running?", "Show me the logs", "Is the cluster healthy?"
- Dry-run operations, diff, plan, preview

WRITE operations (mutating, creating, updating, deleting — changes cluster state):
- Deploy, install, upgrade, uninstall, rollback
- Create, apply, delete, patch resources
- Scale deployments, set replicas
- Helm install, helm upgrade, helm uninstall, helm rollback
- ArgoCD create app, sync app, delete app
- Argo Rollouts promote, abort, retry
- Traefik traffic shifting, canary weight changes
- Cordon, drain, taint nodes
- Create namespaces, secrets, configmaps
- "Deploy nginx", "Scale to 5 replicas", "Delete the namespace"
- Any request that explicitly asks to change, modify, or update resources

If uncertain, classify as "write" (conservative — safety first).
</operation_rules>

<agent_mapping>
- helm_operator: Helm chart operations (install, list, status, upgrade, values, history, template, lint)
- k8s_operator: Direct Kubernetes resource operations (pods, deployments, services, nodes, namespaces, events, logs)
- app_operator: ArgoCD, Argo Rollouts, Traefik (app lifecycle, traffic management, canary, blue-green)
- observability_operator: Prometheus, Alertmanager, Loki, Tempo, OpenTelemetry, Grafana (metrics, logs, traces, alerts)
- general: Greetings, help requests, unclear intent, non-infrastructure queries
</agent_mapping>

<user_message_guidelines>
Generate a `user_message` appropriate for the classification.
Use STANDARD MARKDOWN formatting — the system will convert it to Slack format.
  - Use **bold** (double asterisks) for emphasis.
  - Use `- ` (dash space) for bullet points, each on its OWN line.
  - Use newlines (`\\n`) to separate paragraphs and bullet lists.

For READ operations:
- Write a brief, friendly one-liner acknowledging the request.
  Example: "Looking into your cluster status now..."

For WRITE operations:
- Be specific about what the user asked for (reference their actual request).
- Explain that write operations are not yet available via Slack.
- Suggest 2-3 specific *read-only* alternatives the user CAN do via Slack
  that are relevant to their request (not generic — tailored to their ask).
- Mention that write support is coming based on community feedback.
- Keep the tone friendly, helpful, and empathetic — not robotic.
- Include a :warning: emoji at the start for write blocks.
- Do NOT mention "web UI" — just say write operations aren't available
  via Slack yet.

Example for "deploy nginx to staging":
  ":warning: **Deploying nginx to staging** isn't available via Slack yet — write operations are coming soon based on community feedback!\n\nIn the meantime, here's what I can help with right here:\n- Check if nginx is already running in staging\n- Show the current deployment status and pod health\n- View recent deployment events and logs"
</user_message_guidelines>

<output_contract>
Return a JSON object matching the schema exactly.
All fields including user_message are required.
Do not output natural language outside of the schema fields.
</output_contract>
"""


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """Result of the operation gate classification.

    Attributes:
        allowed: Whether the request should proceed to the graph.
        classification: The LLM's structured classification (if available).
        block_message: Human-readable message when the request is blocked.
    """

    allowed: bool
    classification: Optional[SlackOperationClassification] = None
    block_message: Optional[str] = None





# ---------------------------------------------------------------------------
# SlackOperationGate
# ---------------------------------------------------------------------------


class SlackOperationGate:
    """Pre-graph gate that classifies and filters Slack requests.

    Uses an LLM with structured output (same pattern as the supervisor's
    :class:`RouterDecision`) to classify each incoming message as
    ``read`` or ``write``, and checks the result against the configured
    write whitelist.

    The gate is designed to be **fast** and **fail-open for reads**:
    if classification fails, the request is allowed through so that
    the graph can handle it (the graph has its own HITL gates for
    mutating operations).

    Usage::

        gate = SlackOperationGate(config)
        result = await gate.classify("list pods in default namespace")
        if result.allowed:
            # proceed to graph
        else:
            # send result.block_message to Slack
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._classifier: Any = None  # Lazy-init
        self._operation_mode: str = getattr(
            config, "SLACK_OPERATION_MODE", "read",
        )
        self._write_whitelist: list[str] = getattr(
            config, "SLACK_WRITE_WHITELIST", [],
        )

        logger.info(
            "Operation gate initialized",
            extra={
                "mode": self._operation_mode,
                "whitelist": self._write_whitelist,
            },
        )

    # ── Lazy LLM initialisation ───────────────────────────────────────

    async def _get_classifier(self) -> Any:
        """Lazy-initialize the LLM classifier.

        Uses the same standard LLM config as the supervisor router
        (fast, cost-effective model) to keep classification latency low.
        """
        if self._classifier is None:
            from k8s_autopilot.utils.llm import create_model

            model = create_model(self._config.llm_config)
            self._classifier = model.with_structured_output(
                SlackOperationClassification,
                include_raw=True,
            )
            logger.info("Operation gate classifier initialized")
        return self._classifier

    # ── Public API ────────────────────────────────────────────────────

    async def classify(self, user_text: str) -> GateResult:
        """Classify a user message and return the gate decision.

        Args:
            user_text: The raw message text from Slack.

        Returns:
            :class:`GateResult` with ``allowed=True`` if the request
            should proceed to the LangGraph graph, or ``allowed=False``
            with a ``block_message`` for the user.
        """
        # If mode is "readwrite" → bypass gate entirely
        if self._operation_mode == "readwrite":
            return GateResult(allowed=True)

        # Classify the request via LLM structured output
        classifier = await self._get_classifier()
        messages = [
            SystemMessage(content=GATE_CLASSIFICATION_PROMPT),
            HumanMessage(content=user_text),
        ]

        try:
            response = await classifier.ainvoke(messages)

            # with_structured_output(include_raw=True) returns a dict
            if isinstance(response, dict):
                decision: SlackOperationClassification | None = response.get(
                    "parsed",
                )
            else:
                # Direct structured output (no include_raw)
                decision = response
        except Exception as exc:
            # Fail-open: on classification failure, allow through.
            # The graph has its own HITL gates for write safety.
            logger.warning(
                f"Operation gate classification failed — allowing through: {exc}",
            )
            return GateResult(allowed=True)

        if decision is None:
            logger.warning("Operation gate produced no parsed decision — allowing through")
            return GateResult(allowed=True)

        logger.info(
            "Operation gate classification",
            extra={
                "operation_type": decision.operation_type,
                "agent": decision.agent,
                "reasoning": decision.reasoning[:100],
                "user_text_preview": user_text[:50],
            },
        )

        # Read operations → always allowed
        if decision.operation_type == "read":
            return GateResult(allowed=True, classification=decision)

        # Write operation → check whitelist
        if self._is_agent_whitelisted(decision.agent):
            logger.info(
                "Write operation allowed (agent whitelisted)",
                extra={"agent": decision.agent},
            )
            return GateResult(allowed=True, classification=decision)

        # Write operation blocked
        logger.info(
            "Write operation blocked by operation gate",
            extra={
                "agent": decision.agent,
                "reasoning": decision.reasoning,
            },
        )
        return GateResult(
            allowed=False,
            classification=decision,
            block_message=decision.user_message,
        )

    # ── Internal helpers ──────────────────────────────────────────────

    def _is_agent_whitelisted(self, agent: str) -> bool:
        """Check if an agent is in the write whitelist.

        Supports:
        - ``"*"`` — wildcard, all agents allowed
        - Exact match — ``"observability_operator"``
        - Prefix match — ``"observability_operator"`` matches
          ``"observability_operator.prometheus"`` (future subagent granularity)
        """
        if "*" in self._write_whitelist:
            return True
        return any(
            agent == entry or agent.startswith(f"{entry}.")
            for entry in self._write_whitelist
        )
