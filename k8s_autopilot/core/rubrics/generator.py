"""
Rubric generation from task objectives.

Adapted from dcode's goal_rubric.py — generates concrete, testable
acceptance criteria that the RubricEvaluator uses to grade agent output.

Usage::

    from k8s_autopilot.core.rubrics.generator import generate_rubric
    criteria = generate_rubric("Deploy nginx chart to production namespace")

Reference: dcode/code/goal_rubric.py
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("RubricsGenerator")


RUBRIC_SYSTEM_PROMPT = (
    "You draft acceptance criteria for an agent's task goal.\n\n"
    "Return only a concise markdown bullet list of criteria. "
    "Each criterion should be concrete, testable, and framed "
    "as a definition of done. Include criteria for validation, "
    "scope control, and user-visible behavior when relevant.\n\n"
    "Rules:\n"
    "- Each item starts with a dash and a space (- )\n"
    "- Each item is a single line\n"
    "- 3-7 criteria typically\n"
    "- Focus on observable outcomes, not process steps\n"
    "- Include at least one negative criterion (what should NOT happen)"
)


def generate_rubric(
    objective: str,
    *,
    config: Optional["Config"] = None,
    max_criteria: int = 7,
) -> str:
    """Generate acceptance criteria using the standard-tier (cheap) model.

    This uses the default LLM (not the expensive deep-agent model) to keep
    rubric generation cost-effective. Falls back to a generic rubric if
    model invocation fails.

    Args:
        objective: The task goal / objective to generate criteria for.
        config: Config instance for model resolution.
        max_criteria: Maximum number of criteria to generate.

    Returns:
        A markdown bullet list of acceptance criteria.
    """
    if config is None:
        from k8s_autopilot.config.config import Config
        config = Config()

    try:
        from langchain.chat_models import init_chat_model
        from langchain_core.messages import HumanMessage, SystemMessage

        # Use rubric-specific model, then standard LLM, then hardcoded fallback
        model_name = getattr(config, "RUBRIC_GRADER_MODEL", None) or getattr(
            config, "LLM_MODEL", "gpt-4o-mini"
        )
        provider = getattr(config, "LLM_PROVIDER", "openai")

        model = init_chat_model(
            model_name,
            model_provider=provider,
            temperature=0.0,
            max_tokens=500,
        )

        response = model.invoke([
            SystemMessage(content=RUBRIC_SYSTEM_PROMPT),
            HumanMessage(content=f"<goal>\n{objective}\n</goal>"),
        ])

        criteria = getattr(response, "content", "")
        if isinstance(criteria, str) and criteria.strip():
            # Truncate to max_criteria
            lines = [
                line for line in criteria.strip().split("\n")
                if line.strip().startswith("- ")
            ]
            return "\n".join(lines[:max_criteria])

    except Exception as e:
        logger.warning(f"LLM rubric generation failed: {e}")

    # Fallback generic rubric
    return _generic_rubric(objective)


def _generic_rubric(objective: str) -> str:
    """Generate a generic rubric when LLM generation fails."""
    return (
        f"- The requested task is fully completed: {objective}\n"
        "- No errors or warnings were raised during execution\n"
        "- Output is clear, well-formatted, and actionable\n"
        "- No out-of-scope operations were performed"
    )
