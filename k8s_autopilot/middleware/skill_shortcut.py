"""Skill exists shortcut middleware — skips planner if skills already exist."""

from collections.abc import Callable
import re
from typing import Any

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from k8s_autopilot.middleware.registry import BaseAgentMiddleware, register_middleware
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("SkillExistsShortcut")

# Matches app-specific chart generator skill paths
_SKILL_PATTERN = re.compile(r"^/skills/helm-operator/[\w-]+-chart-generator/SKILL\.md$")

# Sub-agents to hide when skills already exist
_SKIP_WHEN_SKILLS_EXIST = frozenset({"helm-planner", "helm-skill-builder"})


def _apply_skill_shortcut(
    state: Any,
    tools: list[Any],
    messages: list[Any],
) -> tuple:
    """Core logic for skill-exists shortcut — extracted for testability."""
    files = state.get("files", {}) if isinstance(state, dict) else {}

    # Find matching skill files
    matching_skills = [f for f in files if _SKILL_PATTERN.match(f)]

    if not matching_skills:
        return tools, messages, False

    # ── 1. Remove planner/skill-builder from available tools ───────────
    filtered_tools = [t for t in tools if getattr(t, "name", "") not in _SKIP_WHEN_SKILLS_EXIST]

    # ── 2. Inject directive system message (transient) ─────────────────
    skill_names = ", ".join(matching_skills)
    skip_directive = SystemMessage(
        content=(
            f"[SKILL-EXISTS SHORTCUT] Skills already exist for this "
            f"application type: {skill_names}. "
            f"You MUST skip helm-planner and helm-skill-builder entirely. "
            f"Go directly to task(helm-generator). "
            f"Do NOT call helm-planner under any circumstances."
        )
    )
    updated_messages = [*list(messages), skip_directive]

    logger.info(
        "SkillExistsMiddleware: planner/skill-builder removed, directive injected",
        extra={
            "matching_skills": matching_skills,
            "tools_removed": list(_SKIP_WHEN_SKILLS_EXIST),
        },
    )

    return filtered_tools, updated_messages, True


@register_middleware(name="skill_exists_shortcut")
class SkillExistsMiddleware(BaseAgentMiddleware):
    """Skip helm-planner when skills already exist for the requested app type."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Sync wrap_model_call — filters tools and injects directive."""
        filtered_tools, updated_messages, activated = _apply_skill_shortcut(
            state=request.state,
            tools=request.tools,
            messages=request.messages,
        )

        if not activated:
            return handler(request)

        return handler(
            request.override(
                tools=filtered_tools,
                messages=updated_messages,
            )
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable,
    ) -> ModelResponse:
        """Async wrap_model_call — same logic, async handler."""
        filtered_tools, updated_messages, activated = _apply_skill_shortcut(
            state=request.state,
            tools=request.tools,
            messages=request.messages,
        )

        if not activated:
            return await handler(request)

        return await handler(
            request.override(
                tools=filtered_tools,
                messages=updated_messages,
            )
        )


skill_exists_shortcut = SkillExistsMiddleware()
