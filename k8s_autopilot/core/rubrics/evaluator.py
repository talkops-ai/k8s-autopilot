"""
Rubric evaluation engine — grades agent output against acceptance criteria.

Adapted from dcode's goal_rubric.py grader loop. Uses the standard-tier
(cheap) model to evaluate whether each criterion was met, providing
structured pass/fail results with evidence.

Usage::

    from k8s_autopilot.core.rubrics.evaluator import RubricEvaluator, RubricResult

    evaluator = RubricEvaluator()
    result = evaluator.evaluate(criteria_text, messages)
    if not result.all_passed:
        # inject feedback, loop back
"""

from __future__ import annotations

import json
from k8s_autopilot.utils.logger import AgentLogger
from dataclasses import dataclass, field
from typing import Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("RubricsEvaluator")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CriterionResult:
    """Evaluation result for a single acceptance criterion."""

    criterion: str
    passed: bool
    evidence: str


@dataclass
class RubricResult:
    """Aggregate evaluation result for all criteria."""

    criteria: List[CriterionResult] = field(default_factory=list)
    all_passed: bool = False
    feedback: Optional[str] = None

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.criteria if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.criteria if not c.passed)

    @property
    def summary(self) -> str:
        """Human-readable summary of evaluation results."""
        total = len(self.criteria)
        passed = self.passed_count
        return f"{passed}/{total} criteria passed"


# ---------------------------------------------------------------------------
# Grader prompts
# ---------------------------------------------------------------------------

GRADER_SYSTEM_PROMPT = (
    "You are a task completion grader. Given a rubric (acceptance criteria) "
    "and a conversation transcript, evaluate whether each criterion was met.\n\n"
    "Return a JSON array with one object per criterion:\n"
    '  {"criterion": "...", "passed": true/false, "evidence": "..."}\n\n'
    "Rules:\n"
    "- Be strict but fair\n"
    "- Only mark passed=true if there is clear evidence in the conversation\n"
    "- evidence should be a brief quote or reference to what the agent did\n"
    "- If you cannot determine, mark passed=false\n"
    "- Return ONLY the JSON array, no other text"
)


def _build_grader_prompt(criteria: str, messages: List[Any]) -> str:
    """Build the grader prompt with criteria and conversation context."""
    # Extract recent messages for grading context
    context_parts: List[str] = []
    for msg in messages[-30:]:  # Last 30 messages for context
        msg_type = getattr(msg, "type", "")
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content.strip():
            # Truncate individual messages
            if len(content) > 1000:
                content = content[:1000] + "..."
            context_parts.append(f"[{msg_type.upper()}]: {content}")

    conversation = "\n---\n".join(context_parts)

    return (
        f"<rubric>\n{criteria}\n</rubric>\n\n"
        f"<conversation>\n{conversation}\n</conversation>"
    )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class RubricEvaluator:
    """Evaluate agent output against acceptance criteria.

    Uses the standard-tier model (RUBRIC_GRADER_MODEL → LLM_MODEL) to
    grade each criterion independently.

    Args:
        config: Config instance for model resolution.
        max_iterations: Max re-evaluation attempts per session.
    """

    def __init__(
        self,
        *,
        config: Optional["Config"] = None,
        max_iterations: int = 3,
    ) -> None:
        self._config = config
        self.max_iterations = max_iterations
        self._iteration_count: int = 0

    def _get_config(self) -> "Config":
        if self._config is None:
            from k8s_autopilot.config.config import Config
            self._config = Config()
        return self._config

    def evaluate(
        self,
        criteria: str,
        messages: List[Any],
    ) -> RubricResult:
        """Evaluate agent output against rubric criteria.

        Args:
            criteria: Markdown bullet list of acceptance criteria.
            messages: The agent's conversation messages to evaluate.

        Returns:
            RubricResult with per-criterion pass/fail and feedback.
        """
        self._iteration_count += 1

        if self._iteration_count > self.max_iterations:
            logger.warning(f"Rubric evaluator hit max iterations ({self.max_iterations:d}) — auto-passing")
            return RubricResult(
                all_passed=True,
                feedback="Max rubric iterations reached — auto-approved.",
            )

        try:
            return self._evaluate_via_llm(criteria, messages)
        except Exception as e:
            logger.warning(f"LLM rubric evaluation failed: {e}")
            return self._fallback_result(criteria)

    def _evaluate_via_llm(
        self,
        criteria: str,
        messages: List[Any],
    ) -> RubricResult:
        """Run LLM-based evaluation."""
        from langchain.chat_models import init_chat_model
        from langchain_core.messages import HumanMessage, SystemMessage

        cfg = self._get_config()
        model_name = getattr(cfg, "RUBRIC_GRADER_MODEL", None) or getattr(
            cfg, "LLM_MODEL", "gpt-4o-mini"
        )
        provider = getattr(cfg, "LLM_PROVIDER", "openai")

        model = init_chat_model(
            model_name,
            model_provider=provider,
            temperature=0.0,
            max_tokens=1000,
        )

        prompt = _build_grader_prompt(criteria, messages)
        response = model.invoke([
            SystemMessage(content=GRADER_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        raw_content = getattr(response, "content", "")
        return self._parse_grader_response(raw_content, criteria)

    def _parse_grader_response(
        self,
        raw: str,
        criteria: str,
    ) -> RubricResult:
        """Parse LLM grader response JSON into RubricResult."""
        # Strip markdown code fences if present
        content = raw.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

        try:
            items = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Failed to parse grader JSON — falling back")
            return self._fallback_result(criteria)

        if not isinstance(items, list):
            return self._fallback_result(criteria)

        results: List[CriterionResult] = []
        for item in items:
            if isinstance(item, dict):
                results.append(CriterionResult(
                    criterion=item.get("criterion", ""),
                    passed=bool(item.get("passed", False)),
                    evidence=item.get("evidence", ""),
                ))

        all_passed = all(r.passed for r in results) if results else False

        # Generate feedback for failed criteria
        feedback = None
        if not all_passed:
            failed = [r for r in results if not r.passed]
            feedback_lines = [
                f"❌ {r.criterion}\n   Evidence: {r.evidence}"
                for r in failed
            ]
            feedback = (
                f"Rubric evaluation failed ({len(failed)}/{len(results)} criteria not met):\n\n"
                + "\n\n".join(feedback_lines)
            )

        return RubricResult(
            criteria=results,
            all_passed=all_passed,
            feedback=feedback,
        )

    def _fallback_result(self, criteria: str) -> RubricResult:
        """Generate a fallback result when LLM evaluation fails."""
        # Parse criteria lines and mark all as unknown
        lines = [
            line.strip().lstrip("- ")
            for line in criteria.split("\n")
            if line.strip().startswith("- ")
        ]

        results = [
            CriterionResult(
                criterion=line,
                passed=False,
                evidence="Could not evaluate — LLM grading failed",
            )
            for line in lines
        ]

        return RubricResult(
            criteria=results,
            all_passed=False,
            feedback=(
                "Rubric evaluation could not be completed. "
                "Manual review recommended."
            ),
        )

    @property
    def iteration_count(self) -> int:
        """Current iteration count."""
        return self._iteration_count

    def reset(self) -> None:
        """Reset iteration counter for a new session."""
        self._iteration_count = 0
