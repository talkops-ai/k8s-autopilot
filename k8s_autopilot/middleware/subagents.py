"""Subagents middleware — manages subagent metadata and system-prompt injection.

Lifecycle:
1. Constructed with a list of ``SubagentMetadata`` dicts.
2. ``before_agent`` — injects a concise listing of subagent names and
   descriptions into state updates.
3. ``wrap_model_call`` / ``awrap_model_call`` — appends dynamically bifurcated
   system-prompt block for Built-in vs Plugin subagents to system_message.
4. Runtime code calls ``get_subagent(name)`` to look up full metadata
   when the LLM actually invokes the subagent.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage
from deepagents.middleware._utils import append_to_system_message

from k8s_autopilot.middleware.registry import register_middleware

if TYPE_CHECKING:
    from collections.abc import Sequence
    from langgraph.runtime import Runtime
    from k8s_autopilot.subagents.types import SubagentMetadata

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


@register_middleware(name="subagents")
class SubagentsMiddleware(AgentMiddleware):
    """Middleware that manages subagent metadata and system-prompt injection.

    Lifecycle
    ---------
    1. Constructed with a list of ``SubagentMetadata`` dicts.
    2. ``before_agent`` — injects a concise listing of subagent names and
       descriptions into state updates.
    3. ``wrap_model_call`` / ``awrap_model_call`` — appends dynamically bifurcated
       system-prompt block for Built-in vs Plugin subagents to system_message.
    4. Runtime code calls ``get_subagent(name)`` to look up full metadata
       when the LLM actually invokes the subagent.
    """

    def __init__(
        self,
        subagent_metas: Sequence[SubagentMetadata] | None = None,
        planning_mode: bool = False,
    ) -> None:
        super().__init__()
        self._planning_mode = planning_mode
        self._registry: dict[str, SubagentMetadata] = {}
        if subagent_metas:
            for meta in subagent_metas:
                name = meta.get("name", "")
                if name:
                    self._registry[name] = meta

        logger.debug(
            "[SubagentsMiddleware] Initialized with %d subagent(s) (planning_mode=%s): %s",
            len(self._registry),
            self._planning_mode,
            list(self._registry.keys()),
        )

    # ── Public API ──────────────────────────────────────────

    @property
    def subagent_names(self) -> list[str]:
        """Return sorted list of registered subagent names."""
        return sorted(self._registry.keys())

    def get_subagent(self, name: str) -> SubagentMetadata | None:
        """Look up a subagent by exact name."""
        return self._registry.get(name)

    def register_subagent(self, meta: SubagentMetadata) -> None:
        """Add or replace a subagent registration at runtime."""
        name = meta.get("name", "")
        if not name:
            logger.warning("[SubagentsMiddleware] Cannot register subagent without a name")
            return
        self._registry[name] = meta
        logger.debug("[SubagentsMiddleware] Registered subagent: %s", name)

    def list_subagents(self) -> list[SubagentMetadata]:
        """Return all registered subagent metadata dicts."""
        return list(self._registry.values())

    # ── Classification & Prompt building ───────────────────────

    def _is_plugin_subagent(self, meta: SubagentMetadata) -> bool:
        """Determine whether a subagent is plugin-provided based on runtime metadata."""
        name = meta.get("name", "")
        source = str(meta.get("source", ""))
        return (
            "plugin" in source.lower()
            or "@" in name
            or bool(meta.get("is_plugin"))
        )

    def _build_prompt_block(self) -> str:
        """Build the system-prompt fragment dynamically listing available subagents."""
        if not self._registry:
            return ""

        if self._planning_mode:
            lines: list[str] = ["\n\n## Subagent Delegation & Operational Capabilities\n"]
            lines.append(
                "K8s Autopilot operates with specialized autonomous subagents to execute and verify tasks. "
                "You do NOT execute tasks or run subagents yourself. "
                "Use this context solely to understand the operational capabilities and verification boundaries available when drafting acceptance criteria:\n"
            )
            for name in sorted(self._registry):
                meta = self._registry[name]
                desc = meta.get("description", "No description provided.")
                lines.append(f"- **`{name}`**: {desc}")
            return "\n".join(lines)

        builtin_agents: list[SubagentMetadata] = []
        plugin_agents: list[SubagentMetadata] = []

        for name in sorted(self._registry):
            meta = self._registry[name]
            if self._is_plugin_subagent(meta):
                plugin_agents.append(meta)
            else:
                builtin_agents.append(meta)

        lines: list[str] = ["\n\n## Subagent Delegation & Orchestration Architecture\n"]
        lines.append(
            "You have access to specialized subagents for task delegation. "
            "Delegation channels are strictly bifurcated based on subagent origin:"
        )

        # 1. Built-in Subagents
        if builtin_agents:
            lines.append("\n### 1. Built-in Subagents (Direct `task` Tool)")
            lines.append(
                "Use the native `task` tool call directly from chat for these core subagents:\n"
            )
            for meta in builtin_agents:
                name = meta.get("name", "")
                desc = meta.get("description", "No description provided.")
                lines.append(f"- **`{name}`**: {desc}")
            lines.append("\n*Invocation Format:*")
            lines.append('`task(description="...", subagent_type="<subagent_name>")`')

        # 2. Plugin & Extension Subagents
        if plugin_agents:
            lines.append("\n---\n")
            lines.append("### 2. Plugin & Extension Subagents (Code Interpreter `js_eval`)")
            lines.append(
                "Plugin-provided subagents MUST be triggered exclusively through the "
                "`js_eval` Code Interpreter tool using the embedded `task()` JavaScript "
                "primitive.\n"
            )
            for meta in plugin_agents:
                name = meta.get("name", "")
                desc = meta.get("description", "No description provided.")
                source = meta.get("source", "")
                skills = meta.get("skills")

                lines.append(f"- **`{name}`**: {desc}")
                if source:
                    lines.append(f"  - Source: `{source}`")
                if skills:
                    skill_str = ", ".join(f"`{s}`" for s in skills)
                    lines.append(f"  - Skills: {skill_str}")

            lines.append("\n*Invocation Format (via `js_eval`):*")
            lines.append("```javascript")
            lines.append("const result = await task({")
            lines.append('  description: "...",')
            lines.append('  subagentType: "<subagent_name@plugin_id>",')
            lines.append("  responseSchema: { ... }")
            lines.append("});")
            lines.append("return result;")
            lines.append("```")

        # 3. Automatic Routing Rules
        lines.append("\n---\n")
        lines.append("### Automatic Routing Rules (Zero User Intervention)\n")
        lines.append("1. **Auto-Detect Origin**: Check the dynamically generated lists above:")
        lines.append(
            "   - If the target subagent is listed under **Built-in Subagents**, "
            "call `task(...)` directly."
        )
        lines.append(
            "   - If the target subagent is listed under **Plugin & Extension Subagents**, "
            "call `js_eval(...)` running `await task({...})`."
        )
        lines.append(
            "2. **Never Ask the User for Execution Method**: Do NOT prompt the user "
            "asking whether to use `task` or `js_eval`. Execute the correct route "
            "automatically based on subagent origin."
        )
        lines.append(
            "3. **Multi-File / Batch Processing**: When operating on multiple files or "
            "resources with plugin subagents, run a single `js_eval` script using "
            "`Promise.all` with bounded concurrency (batch size <= 10).\n"
        )

        return "\n".join(lines)

    # ── Middleware hooks (matching AgentMiddleware signature) ──

    def before_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Inject subagent registry metadata into state for reference by tools."""
        if not self._registry:
            return None

        return {
            "_subagent_registry": {
                name: {
                    "name": meta.get("name", ""),
                    "description": meta.get("description", ""),
                    "source": meta.get("source", ""),
                    "skills": meta.get("skills"),
                    "tools": meta.get("tools"),
                    "permission_tier": meta.get("permission_tier"),
                    "is_plugin": self._is_plugin_subagent(meta),
                }
                for name, meta in self._registry.items()
            },
        }

    async def abefore_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Async variant delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject the subagent prompt block into system_message before model execution."""
        block = self._build_prompt_block()
        if block and request.system_message is not None:
            new_system_msg = append_to_system_message(request.system_message, block)
            request = request.override(system_message=new_system_msg)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async variant of wrap_model_call."""
        block = self._build_prompt_block()
        if block and request.system_message is not None:
            new_system_msg = append_to_system_message(request.system_message, block)
            request = request.override(system_message=new_system_msg)
        return await handler(request)
