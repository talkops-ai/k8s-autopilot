"""
Rubric Evaluator Middleware — intercepts completion and evaluates against rubric.

Follows dcode's RubricMiddleware pattern:
  1. Agent claims completion via pending_goal_completion_note
  2. Grader model evaluates output against rubric criteria
  3. If any criteria fail → inject feedback, loop back
  4. If all pass → allow completion
  5. Cap at max_iterations to prevent infinite loops

Registered as ``rubric_evaluator`` in the middleware registry.

Usage in coordinator::

    custom_mws = get_middleware_registry().build_middlewares([
        "operation_context",
        ("rubric_evaluator", {"max_iterations": 3}),
    ])
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
from typing import Any, Optional

from k8s_autopilot.core.middleware.registry import (
    BaseAgentMiddleware,
    register_middleware,
)

logger = AgentLogger("RubricsMW")


@register_middleware(name="rubric_evaluator")
class RubricEvaluatorMiddleware(BaseAgentMiddleware):
    """Intercept agent completion and evaluate against rubric criteria.

    This middleware participates in the goal-driven self-repair loop:

    1. A rubric is generated at task start (via ``generate_rubric()``)
    2. When the agent claims completion, this middleware intercepts
    3. It evaluates the conversation against the rubric criteria
    4. If criteria fail: injects feedback as a SystemMessage and loops back
    5. If all pass: allows completion to proceed
    6. After ``max_iterations`` failures: auto-passes to prevent loops

    Args:
        max_iterations: Max number of re-evaluation attempts.
        rubric: Pre-generated rubric criteria (markdown bullet list).
            If None, rubric is generated from the first HumanMessage.
        config: Config instance for model resolution.
    """

    def __init__(
        self,
        *,
        max_iterations: Optional[int] = None,
        rubric: Optional[str] = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._config = config
        self._rubric = rubric
        self._evaluator: Optional[Any] = None

        # Resolve max_iterations from config
        cfg = self._get_config()
        self._max_iterations = max_iterations or getattr(
            cfg, "RUBRIC_MAX_ITERATIONS", 3
        )

    def _get_config(self) -> Any:
        if self._config is None:
            try:
                from k8s_autopilot.config.config import Config
                self._config = Config()
            except Exception:
                pass
        return self._config

    def _get_evaluator(self) -> Any:
        if self._evaluator is None:
            from k8s_autopilot.core.rubrics.evaluator import RubricEvaluator
            self._evaluator = RubricEvaluator(
                config=self._get_config(),
                max_iterations=self._max_iterations,
            )
        return self._evaluator

    def _get_or_generate_rubric(self, messages: list) -> str:
        """Get pre-set rubric or generate from the first HumanMessage."""
        if self._rubric:
            return self._rubric

        # Extract objective from first human message
        for msg in messages:
            if getattr(msg, "type", "") == "human":
                content = getattr(msg, "content", "")
                if isinstance(content, str) and content.strip():
                    from k8s_autopilot.core.rubrics.generator import generate_rubric
                    self._rubric = generate_rubric(
                        content,
                        config=self._get_config(),
                    )
                    logger.info(f"Generated rubric: {self._rubric}")
                    return self._rubric

        # Fallback: generic rubric
        self._rubric = (
            "- The requested task was completed successfully\n"
            "- No errors occurred during execution"
        )
        return self._rubric

    def evaluate_completion(
        self,
        messages: list,
    ) -> dict[str, Any] | None:
        """Evaluate whether the agent's output meets the rubric criteria.

        Args:
            messages: Current conversation messages.

        Returns:
            None if all criteria pass (allow completion).
            Dict with feedback messages to inject if criteria fail.
        """
        rubric = self._get_or_generate_rubric(messages)
        evaluator = self._get_evaluator()

        result = evaluator.evaluate(rubric, messages)

        if result.all_passed:
            logger.info(f"Rubric evaluation passed ({result.summary}) — iteration {evaluator.iteration_count:d}")
            return None

        # Criteria failed — inject feedback
        logger.info(f"Rubric evaluation failed ({result.summary}) — iteration {evaluator.iteration_count:d}/{self._max_iterations:d}")

        from langchain_core.messages import SystemMessage

        feedback_msg = SystemMessage(content=(
            f"⚠️ **Rubric Evaluation Failed** ({result.summary}):\n\n"
            f"{result.feedback}\n\n"
            "Please address the failed criteria and try again. "
            "Do not claim completion until all criteria are met."
        ))

        return {"feedback_message": feedback_msg, "result": result}

    @property
    def rubric(self) -> Optional[str]:
        """Return the current rubric text."""
        return self._rubric

    @rubric.setter
    def rubric(self, value: str) -> None:
        """Set rubric criteria explicitly."""
        self._rubric = value

    def reset(self) -> None:
        """Reset the evaluator for a new task."""
        self._rubric = None
        if self._evaluator:
            self._evaluator.reset()
