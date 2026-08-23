"""
Goal-driven rubrics and self-repair loops.

Provides a rubric evaluation system adapted from dcode's goal_rubric.py:
  - ``generate_rubric()`` — generates acceptance criteria from objectives
  - ``RubricEvaluator`` — grades agent output against criteria
  - ``RubricEvaluatorMiddleware`` — hooks into the middleware registry
"""

from k8s_autopilot.core.rubrics.generator import generate_rubric
from k8s_autopilot.core.rubrics.evaluator import (
    CriterionResult,
    RubricEvaluator,
    RubricResult,
)

__all__ = [
    "CriterionResult",
    "RubricEvaluator",
    "RubricResult",
    "generate_rubric",
]
