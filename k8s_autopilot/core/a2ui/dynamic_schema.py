"""Dynamic A2UI schema generation tool.

Follows the CopilotKit dynamic-schema pattern:

1. The supervisor agent exposes ``generate_a2ui`` as a LangChain tool.
2. When invoked, ``generate_a2ui`` calls a **secondary** LLM with
   ``render_a2ui`` bound as a forced tool-call, prompting it to design
   a dynamic UI layout from the conversation context.
3. The secondary LLM returns a ``render_a2ui`` tool-call whose arguments
   (surfaceId, catalogId, components, data) are converted into A2UI v0.9
   operations via ``copilotkit.a2ui`` helpers.
4. The resulting JSON string (``{"a2ui_operations": [...]}``) is returned
   as the tool result, which the A2UI middleware in the executor detects
   and forwards to the frontend renderer.

Reference:
    CopilotKit showcase/integrations/langgraph-python/src/agents/a2ui_dynamic.py
"""

from __future__ import annotations

import json
import logging
from typing import Any

from copilotkit import a2ui
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, Field

from k8s_autopilot.utils.llm import create_model

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

CUSTOM_CATALOG_ID = (
    "https://github.com/google/A2UI/blob/main/specification/"
    "v0_9/json/standard_catalog_definition.json"
)
"""Standard v0.9 A2UI catalog used by the K8s Autopilot frontend."""

# ── render_a2ui — the tool schema the secondary LLM must call ─────────

class RenderA2UIInput(BaseModel):
    """Input schema for the render_a2ui tool the secondary LLM calls."""
    surfaceId: str = Field(
        description="Unique surface identifier, e.g. 'dynamic-dashboard'.",
    )
    catalogId: str = Field(
        description="The catalog ID for component resolution.",
    )
    components: list[dict[str, Any]] = Field(
        description=(
            "A2UI v0.9 component array (flat format). Each element has 'id' "
            "and 'component' keys. The root component's id must be 'root'."
        ),
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional initial data model for the surface.",
    )


@langchain_tool(args_schema=RenderA2UIInput)
def render_a2ui(
    surfaceId: str,
    catalogId: str,
    components: list[dict[str, Any]],
    data: dict[str, Any] | None = None,
) -> str:
    """Render a dynamic A2UI v0.9 surface.

    This tool is never called directly — it exists solely so the secondary
    LLM can express its UI design as a structured tool-call.
    """
    return "rendered"


# ── build_a2ui_operations ─────────────────────────────────────────────

def build_a2ui_operations_from_tool_call(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Convert the secondary LLM's ``render_a2ui`` tool-call args into an
    ``a2ui_operations`` container the A2UI middleware can detect.
    """
    surface_id = args.get("surfaceId", "dynamic-surface")
    catalog_id = args.get("catalogId", CUSTOM_CATALOG_ID)
    components = args.get("components", [])
    data = args.get("data")

    if not components:
        logger.warning(
            "build_a2ui_operations_from_tool_call: empty components for surface %s",
            surface_id,
        )

    ops: list[dict[str, Any]] = [
        a2ui.create_surface(surface_id, catalog_id=catalog_id),
        a2ui.update_components(surface_id, components),
    ]
    if data:
        ops.append(a2ui.update_data_model(surface_id, data))

    return {a2ui.A2UI_OPERATIONS_KEY: ops}


# ── generate_a2ui — the public tool registered with the supervisor ────

def create_generate_a2ui_tool(config: dict[str, Any] | None = None):
    """Factory that creates the ``generate_a2ui`` LangChain tool.

    The tool captures a ``config`` dict at creation time so it can
    instantiate the secondary LLM via the existing ``create_model`` factory
    (honouring the project's LLM configuration — no hardcoded API keys).

    Parameters
    ----------
    config : dict
        LLM configuration dict (from ``Config.get_llm_config()``).
        If None, falls back to a default model.
    """

    @langchain_tool
    def generate_a2ui(context: str = "") -> str:
        """Generate dynamic A2UI components based on the conversation.

        A secondary LLM designs the UI schema and data. The result is
        returned as an ``a2ui_operations`` container for the A2UI
        middleware to detect and forward to the frontend renderer.

        Args:
            context: Optional conversation context to guide the secondary
                LLM's UI design.
        """
        # ── 1. Create the secondary LLM ──────────────────────────────
        llm_cfg = config or {}
        try:
            secondary_llm = create_model(llm_cfg)
        except Exception:
            logger.exception("Failed to create secondary LLM for generate_a2ui")
            return json.dumps({"error": "Failed to initialise secondary LLM"})

        # ── 2. Bind render_a2ui as forced tool choice ────────────────
        model_with_tool = secondary_llm.bind_tools(
            [render_a2ui],
            tool_choice="render_a2ui",
        )

        # ── 3. Invoke ────────────────────────────────────────────────
        system_content = context or "Generate a useful dashboard UI."
        try:
            response = model_with_tool.invoke(
                [SystemMessage(content=system_content)],
            )
        except Exception:
            logger.exception("Secondary LLM invocation failed")
            return json.dumps({"error": "Secondary LLM invocation failed"})

        # ── 4. Extract tool-call args ────────────────────────────────
        if not getattr(response, "tool_calls", None):
            return json.dumps({"error": "LLM did not call render_a2ui"})

        tool_call = response.tool_calls[0]
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        # ── 5. Build A2UI operations ─────────────────────────────────
        result = build_a2ui_operations_from_tool_call(args)
        return json.dumps(result)

    return generate_a2ui


# ── Observability-specific dynamic schema tool ────────────────────────

OBS_DYNAMIC_SCHEMA_CONTEXT = """You are generating A2UI v0.9 component trees for Kubernetes observability dashboards.

Available custom components (in addition to built-in Card, Column, Row, Text, List, Tabs, Button, Icon, Divider):
- MetricChart: Time-series chart. Props: chartType (line|bar|area), title, xAxisLabel, yAxisLabel, series (path→data), timeRange (path→data)
- DataTable: Searchable paginated table. Props: columns (path→data), rows (path→data), searchable (bool), pageSize (int), title
- TraceTimeline: Gantt waterfall for traces. Props: spans (path→data), focusSpanId, showMiniMap (bool), serviceName, title
- StatusIndicator: Severity badge. Props: severity (critical|warning|info|success|none), label, value

Use path bindings ({"path": "/field"}) for dynamic data. Use literal strings for static labels.
Always include a root component with id="root". Use Tabs for multi-section layouts.
"""


def create_generate_obs_a2ui_tool(config: dict[str, Any] | None = None):
    """Factory that creates the ``generate_obs_a2ui`` LangChain tool.

    A variant of ``create_generate_a2ui_tool`` that injects observability-
    specific component documentation (MetricChart, DataTable, TraceTimeline,
    StatusIndicator) into the secondary LLM's system prompt.

    Used by the coordinator for novel/complex layouts that don't fit the
    fixed-schema templates.

    Parameters
    ----------
    config : dict
        LLM configuration dict (from ``Config.get_llm_config()``).
        If None, falls back to a default model.
    """
    from k8s_autopilot.core.a2ui.catalog_manager import OBSERVABILITY_CATALOG_ID

    @langchain_tool
    def generate_obs_a2ui(context: str = "") -> str:
        """Generate a dynamic A2UI observability dashboard.

        A secondary LLM designs the UI layout using both built-in
        components (Card, Column, Tabs, etc.) and custom observability
        components (MetricChart, DataTable, TraceTimeline, StatusIndicator).

        Use this for novel layouts not covered by the fixed templates
        (e.g., a custom comparison dashboard or an anomaly-focused view).

        Args:
            context: Conversation context and data to guide the
                secondary LLM's UI design.
        """
        llm_cfg = config or {}
        try:
            secondary_llm = create_model(llm_cfg)
        except Exception:
            logger.exception("Failed to create secondary LLM for generate_obs_a2ui")
            return json.dumps({"error": "Failed to initialise secondary LLM"})

        model_with_tool = secondary_llm.bind_tools(
            [render_a2ui],
            tool_choice="render_a2ui",
        )

        system_content = OBS_DYNAMIC_SCHEMA_CONTEXT + "\n\n" + (context or "Generate an observability dashboard.")
        try:
            response = model_with_tool.invoke(
                [SystemMessage(content=system_content)],
            )
        except Exception:
            logger.exception("Secondary LLM invocation failed for obs A2UI")
            return json.dumps({"error": "Secondary LLM invocation failed"})

        if not getattr(response, "tool_calls", None):
            return json.dumps({"error": "LLM did not call render_a2ui"})

        tool_call = response.tool_calls[0]
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        # Override catalog ID to use observability catalog
        args["catalogId"] = OBSERVABILITY_CATALOG_ID

        result = build_a2ui_operations_from_tool_call(args)
        return json.dumps(result)

    return generate_obs_a2ui
