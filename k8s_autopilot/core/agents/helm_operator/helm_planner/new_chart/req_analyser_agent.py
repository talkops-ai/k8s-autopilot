"""
Requirements Analyser Agent — first sub-agent of the Helm planner pipeline.

Re-uses the existing parser tools from the planning swarm:
  - parse_requirements (LLM chain → ParsedRequirements)
  - classify_complexity (LLM chain → ComplexityClassification)
  - validate_requirements (LLM chain → ValidationResult)

These tools are full implementations with Pydantic output schemas, prompt
templates, and LLM chains. They read from ``HelmPlannerState.handoff_data``
and write back via ``Command(update={...})``.

Pipeline: ReqAnalyserAgent → [handoff] → ArchitecturePlannerAgent

Reference: k8s_autopilot/core/agents/helm_generator/planner/planning_swarm.py
"""

from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langgraph.graph.state import CompiledStateGraph

from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerState
from k8s_autopilot.core.agents.types import SubAgent

# Import the EXISTING production tools — no new tool definitions here
from k8s_autopilot.core.agents.helm_operator.helm_planner.new_chart.req_analyser_tool.req_analyser_tool import (
    parse_requirements,
    classify_complexity,
    validate_requirements,
)
from k8s_autopilot.core.agents.helm_operator.helm_planner.new_chart.shared.hitl import create_hitl_tool
REQUIREMENT_ANALYZER_SUBAGENT_PROMPT = """
You are a Helm chart requirements analyzer. Your job: parse requirements → classify complexity → validate data.

## 🎯 PROACTIVE GAP HANDLING & PRIORITY (Step 0)
If the user's initial input is extremely sparse and clearly missing core details, **call `request_human_input` FIRST** before parsing.
1. **CRITICAL**: Container Image (registry/repo/tag), Exposure (Ingress/LB)
2. **IMPORTANT**: Namespace, Replicas, Resources (CPU/Mem), Config/Secrets
3. **OPTIONAL**: Storage, Health Checks
4. **SPECIAL NOTES**: Always explicitly ask the user: "Special Notes: Do you have any additional details or special considerations that weren't covered?"
If missing, ask for them clearly using `request_human_input`. Start with "✅ EXTRACTED: [Found items]" to show understanding, then list your questions, and always include the Special Notes question at the end.

## TOOLS (Use in order)

0. **request_human_input**: Gap Check. Ask user for missing required critical details.
1. **parse_requirements**: Extract app type, framework, language, databases, external services, deployment config, security from user input.
   *CRITICAL*: If you received clarification via `request_human_input` or supervisor feedback, you MUST pass `additional_requirements` (the user's answers/feedback) and `questions_asked` (the questions that were asked) into this tool!
2. **classify_complexity**: Assess if deployment is simple/medium/complex based on component count, features, security needs. 
   *CRITICAL*: If there are supervisor feedback or context considerations, pass them into the `notes` parameter.
3. **validate_requirements**: Check completeness. Flag missing fields, conflicts, or clarifications needed.
   *CRITICAL*: If there are supervisor feedback or context considerations, pass them into the `notes` parameter.
4. **transfer_to_architecture_planner**: CRITICAL MANDATORY STEP.
   You MUST call this tool as your final action to hand off control to the next agent.
   Do NOT ask the user if they want to proceed, just call the tool.

## WORKFLOW (DO NOT DEVIATE)

0. (If obvious critical gaps found instantly) Call request_human_input
1. Call parse_requirements (with HITL context if available)
2. Call classify_complexity (passing supervisor notes if available)
3. Call validate_requirements (passing supervisor notes if available)
4. Call transfer_to_architecture_planner (MANDATORY FINAL STEP)

## KEY RULES

- Always follow the sequence above
- If validate_requirements returns valid=false, you **do not** need to call request_human_input yourself, the internal middleware will automatically handle asking specific clarification questions! Just proceed with the response.
- Do NOT proceed to final answer until validation passes
- For unclear inputs, use tool outputs to guide your questions
- When calling ANY tool with a `notes` optional argument, use it to accurately forward any environmental, supervisor, or HITL contextual observations so those tools can incorporate them into their formatting!

## PRESENTATION (Final Answer Format)

### Requirements Parsed
- App Type: [type]
- Framework/Language: [values]
- Databases: [list]
- Services: [if any]
- Deployment: [replicas, regions, HA]
- Security: [key settings]

### Complexity Assessment
- Level: [simple/medium/complex]
- Key factors: [2-3 main drivers]

### Validation Status
[If valid: "Ready for chart generation"]
[If invalid: List issues + wait for middleware to intercept]
"""

logger = AgentLogger("ReqAnalyserAgent")


class ValidationHITLMiddleware(AgentMiddleware):
    """
    Middleware to dynamically intercept tool commands returning validation clarifications.
    If 'clarifications_needed' are found in the handoff_data, it pauses the graph via
    interrupt() and requests human feedback, seamlessly routing the answer into the state.
    """
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)

        # Only intercept the validate_requirements tool, as it is the only tool that actively
        # generates clarifications. If we inspect all tools, we risk infinitely re-triggering
        # on stale state data preserved by other tools.
        if request.tool_call["name"] != "validate_requirements":
            return result

        if isinstance(result, Command) and result.update and isinstance(result.update, dict):
            handoff_data = result.update.get("handoff_data", {})
            
            # Check for validation clarifications recursively
            validation_result = handoff_data.get("validation_result", {})
            clarifications = validation_result.get("clarifications_needed", [])
            
            if clarifications:
                formatted_clarifications = [f"{i}. {c.strip()}" for i, c in enumerate(clarifications, 1)]
                formatted_questions = "\n".join(formatted_clarifications)
                
                state = request.runtime.state
                
                interrupt_payload = {
                    "pending_feedback_requests": {
                        "status": "input_required",
                        "session_id": state.get("session_id", "unknown"),
                        "question": f"Please clarify the following requirements to finalize the plan:\n\n{formatted_questions}",
                        "context": "Validation identified missing required information.",
                        "active_phase": state.get("current_step", "req_analyser"),
                        "tool_name": request.tool_call["name"],
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                }
                
                logger.info(
                    "Triggering HITL for clarifications dynamically via middleware",
                    extra={"tool": request.tool_call["name"], "clarifications_count": len(clarifications)}
                )
                
                user_feedback = interrupt(interrupt_payload)
                
                if user_feedback:
                    new_reqs = (
                        f"**validation_question_asked**:\n{formatted_questions}\n\n"
                        f"**validation_response**:\n{user_feedback}"
                    )
                    
                    if state.get("updated_user_requirements"):
                        new_reqs = f"{state.get('updated_user_requirements')}\n\n---\n\n{new_reqs}"
                        
                    result.update["updated_user_requirements"] = new_reqs
                    
                    # Update question_asked in state to prevent the validation tool from asking it again
                    old_questions = state.get("question_asked", "")
                    if old_questions:
                        result.update["question_asked"] = f"{old_questions}\n\n{formatted_questions}"
                    else:
                        result.update["question_asked"] = formatted_questions
                                
        return result

class ReqAnalyserAgent(SubAgent):
    """Agent that orchestrates the requirements analysis pipeline.

    Wraps the existing parser tools (parse_requirements, classify_complexity,
    validate_requirements) into the ``SubAgent`` contract so it can be mounted
    as a node in the ``HelmPlannerSupervisorAgent`` StateGraph.

    The tools themselves contain full LLM chain implementations with Pydantic
    schemas and prompt templates — this agent only provides the graph wrapper.

    Reference: k8s_autopilot planning_swarm.py requirement_analyzer_agent
    """

    def __init__(
        self,
        model: BaseChatModel,
        extra_tools: Optional[List[BaseTool]] = None,
    ):
        """
        Args:
            model: The language model for the agent's reasoning loop.
            extra_tools: Additional tools (e.g. handoff tools from the supervisor).
        """
        self.model = model
        self.extra_tools = extra_tools or []

    # -- SubAgent contract -------------------------------------------------

    @property
    def name(self) -> str:
        return "requirements_analyser"

    @property
    def description(self) -> str:
        return REQUIREMENT_ANALYZER_SUBAGENT_PROMPT

    def get_tools(self) -> List[BaseTool]:
        """Return the existing parser tools + any handoff tools.

        Uses the pre-built tools from:
            k8s_autopilot.core.agents.helm_generator.planner.tools.parser
        """
        hitl_tool = create_hitl_tool(default_phase="req_analyser", logger_name="ReqAnalyserHITL")
        base_tools: List[BaseTool] = [
            hitl_tool,
            parse_requirements,
            classify_complexity,
            validate_requirements,
        ]
        return base_tools + self.extra_tools

    def build_agent(self) -> CompiledStateGraph:
        """Build the LangGraph agent via ``create_agent``."""
        return create_agent(
            model=self.model,
            tools=self.get_tools(),
            name=self.name,
            state_schema=HelmPlannerState,
            system_prompt=self.description,
            middleware=[ValidationHITLMiddleware()],
        )
