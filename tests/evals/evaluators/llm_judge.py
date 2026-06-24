from typing import Any, Dict, NamedTuple
from agentevals.trajectory import create_async_trajectory_llm_as_judge


class EvalResult(NamedTuple):
    passed: bool
    score: float
    rationale: str


def create_trajectory_judge(llm, *, feedback_key: str = "trajectory_score", domain: str = ""):
    """
    Creates a trajectory evaluator using agentevals.

    Generalized version of the former ``create_helm_trajectory_judge`` — now
    accepts any domain name so it can be reused for App Operator, Helm, or
    any future operator.

    Args:
        llm: The judge LLM to use for evaluation.
        feedback_key: LangSmith feedback key for tracking results over time.
                      Use domain-specific keys (e.g. ``app_operator_trajectory_score``)
                      to keep experiment runs separate.
        domain: Optional domain label to include in the prompt for context
                (e.g. ``"App Operator (ArgoCD/Rollouts/Traefik)"``).
    """
    domain_context = f"Domain: {domain}\n" if domain else ""
    prompt = f"""You are an expert AI evaluator grading an agent's trace.
{domain_context}
Scenario description: {{inputs[description]}}
User request: {{inputs[user_request]}}
Expectations: {{inputs[expectations]}}

Agent Trace:
{{outputs}}

Did the agent successfully fulfill the user request and adhere to all expectations (including safety)?
Provide a binary score (1 for pass, 0 for fail) and your reasoning.
"""
    return create_async_trajectory_llm_as_judge(
        prompt=prompt,
        judge=llm,
        continuous=False,
        use_reasoning=True,
        feedback_key=feedback_key,
    )


# Backward-compatible alias — existing Helm tests use this name
def create_helm_trajectory_judge(llm):
    """
    Helm Operator trajectory judge.

    Preserved for backward compatibility. Delegates to the generalized
    ``create_trajectory_judge`` with Helm-specific feedback key.
    """
    return create_trajectory_judge(
        llm,
        feedback_key="helm_trajectory_score",
        domain="Helm Operator",
    )


async def evaluate_agent_trajectory(
    llm, trace: Dict[str, Any], scenario: Dict[str, Any],
    *,
    feedback_key: str = "trajectory_score",
    domain: str = "",
) -> EvalResult:
    """
    Uses an LLM as a judge to evaluate if the agent's overall behavior met
    the scenario expectations.

    Args:
        llm: Judge LLM.
        trace: Trajectory dict from the eval runner.
        scenario: Parsed YAML scenario dict.
        feedback_key: LangSmith feedback key (use domain-specific keys).
        domain: Domain label for the judge prompt context.
    """
    judge = create_trajectory_judge(llm, feedback_key=feedback_key, domain=domain)

    import json
    trace_str = json.dumps(trace, indent=2)

    result = await judge(
        inputs=scenario,
        outputs=trace_str,
        reference_outputs=None,
    )

    score = result.get("score", 0.0)
    passed = score >= 0.5
    rationale = result.get("comment", "") or result.get("reasoning", "No rationale provided.")

    return EvalResult(
        passed=passed,
        score=float(score),
        rationale=str(rationale),
    )
