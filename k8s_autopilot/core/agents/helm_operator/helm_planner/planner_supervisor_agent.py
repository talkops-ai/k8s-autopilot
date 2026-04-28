"""
Helm Planner Supervisor Agent (BaseSubgraphAgent).

Two-phase planner pipeline that mirrors the existing planning_swarm.py:

    1. ``requirements_analyser`` — parse → classify → validate
    2. ``architecture_planner``  — analyze → design → resources → scaling → deps

Built as a ``StateGraph`` with ``Command``-based handoffs using
``Command(graph=Command.PARENT)`` so sub-agents transfer control via
handoff tools — identical to the reference pattern.

Re-uses all existing tools and prompts — no new tool definitions.

Reference:
  - k8s_autopilot/core/agents/helm_generator/planner/planning_swarm.py
  - aws-orchestrator PlannerSupervisorAgent (StateGraph + Command + handoff tools)
"""


import json
from typing import Any, Dict, Literal, List, Optional, Union, cast

from langchain.tools import tool, ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.core.state.helm_planner_state import (
    HelmPlannerState,
    HelmPlannerWorkflowState,
)
from k8s_autopilot.core.agents.types import BaseSubgraphAgent

# Import the sub-agents (thin wrappers over existing tools)
from .new_chart.req_analyser_agent import ReqAnalyserAgent
from .new_chart.architecture_planner_agent import ArchitecturePlannerAgent

logger = AgentLogger("HelmPlannerSupervisorAgent")


# ============================================================================
# HelmPlannerSupervisorAgent
# ============================================================================

class HelmPlannerSupervisorAgent(BaseSubgraphAgent):
    """Two-phase planner pipeline built as a StateGraph with Command handoffs.

    Phase 1: ``requirements_analyser``
        parse_requirements → classify_complexity → validate_requirements
        (+ HITL interrupt if clarifications needed)

    Phase 2: ``architecture_planner``
        analyze_application_requirements → design_kubernetes_architecture →
        estimate_resources → define_scaling_strategy → check_dependencies

    Handoff tools use ``Command(goto=..., graph=Command.PARENT)`` enabling
    sub-agents to transfer control via tool calls — the graph routes based
    on ``workflow_state.next_agent``.

    Reference: aws-orchestrator PlannerSupervisorAgent
    """

    def __init__(
        self,
        config: Optional[Any] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        name: str = "helm_planner_supervisor",
        memory: Optional[MemorySaver] = None,
    ):
        logger.info("Initializing HelmPlannerSupervisorAgent")

        if config is None:
            from k8s_autopilot.config.config import Config
            config = Config(custom_config or {})

        self.config = config
        self._name = name
        self._memory = memory or MemorySaver()

        # Initialize models
        from k8s_autopilot.utils.llm import create_model
        self.model = create_model(self.config.get_llm_config())

        # Build sub-agent graphs (with handoff tools injected)
        self._initialize_sub_agents()

        logger.info(
            "HelmPlannerSupervisorAgent initialized",
            extra={"name": name, "phases": 2},
        )

    # -- BaseSubgraphAgent properties --------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def state_model(self) -> Any:
        return HelmPlannerState  # type: ignore[override]

    @property
    def memory(self) -> MemorySaver:
        return self._memory

    @memory.setter
    def memory(self, value: MemorySaver) -> None:
        self._memory = value

    # -- Helper ------------------------------------------------------------

    @staticmethod
    def _coerce_workflow_state(state: Any) -> HelmPlannerWorkflowState:
        """Coerce state['workflow_state'] to HelmPlannerWorkflowState."""
        existing = state.get("workflow_state")
        if isinstance(existing, HelmPlannerWorkflowState):
            return existing
        if isinstance(existing, dict):
            return HelmPlannerWorkflowState(**existing)
        return HelmPlannerWorkflowState()

    # -- Sub-agent initialization (handoff tools + build) ------------------

    def _initialize_sub_agents(self) -> None:
        """Create handoff tools and build each sub-agent graph."""

        _coerce = self._coerce_workflow_state

        # ----------------------------------------------------------------
        # Handoff: requirements_analyser → architecture_planner
        # ----------------------------------------------------------------
        @tool
        def transfer_to_architecture_planner(
            runtime: ToolRuntime[None, HelmPlannerState],
        ) -> Command:
            """Transfer from requirements analyser to architecture planner agent.

            Call this tool AFTER completing requirements analysis (parse,
            classify, validate) to hand off to the architecture planning phase.
            """
            wf = _coerce(runtime.state)
            wf.req_analyser_complete = True
            wf.last_agent = "requirements_analyser"
            wf.next_agent = "architecture_planner"
            wf.current_phase = "architecture_planner"

            logger.info(
                "Handoff: requirements_analyser → architecture_planner",
                extra={"workflow_progress": wf.get_workflow_progress()},
            )

            last_ai = next(
                msg for msg in reversed(runtime.state["messages"])
                if isinstance(msg, AIMessage)
            )
            transfer_msg = ToolMessage(
                content="Requirements analysis complete. Transferring to architecture planner.",
                tool_call_id=runtime.tool_call_id,
            )
            return Command(
                goto="architecture_planner",
                update={
                    "active_agent": "architecture_planner",
                    "current_step": "architecture_planner",
                    "workflow_state": wf.model_dump(),
                    "status": "in_progress",
                    "messages": [last_ai, transfer_msg],
                    "handoff_data": runtime.state.get("handoff_data", {}),
                    "chart_plan": runtime.state.get("chart_plan", {}),
                    "files": runtime.state.get("files", {}),
                    "question_asked": runtime.state.get("question_asked", ""),
                    "updated_user_requirements": runtime.state.get("updated_user_requirements", ""),
                },
                graph=Command.PARENT,
            )

        # ----------------------------------------------------------------
        # Handoff: architecture_planner → END (or reroute on incomplete)
        # ----------------------------------------------------------------
        @tool
        def complete_workflow(
            runtime: ToolRuntime[None, HelmPlannerState],
        ) -> Command:
            """Complete the planning workflow after architecture planning.

            IMPORTANT: Only ends the graph when ALL phases are complete.
            Call this tool AFTER completing architecture planning (analyze,
            design, resources, scaling, deps).
            """
            wf = _coerce(runtime.state)
            wf.architecture_planner_complete = True
            wf.last_agent = "architecture_planner"
            wf.next_agent = None
            wf.workflow_complete = wf.is_complete
            wf.current_phase = cast(
                Literal["req_analyser", "architecture_planner", "complete"],
                "complete" if wf.is_complete else (wf.next_phase or "architecture_planner"),
            )

            logger.info(
                "Attempting workflow completion",
                extra={
                    "workflow_complete": wf.is_complete,
                    "workflow_progress": wf.get_workflow_progress(),
                },
            )

            last_ai = next(
                msg for msg in reversed(runtime.state["messages"])
                if isinstance(msg, AIMessage)
            )

            # If something is missing, reroute to the next incomplete phase
            if not wf.is_complete:
                next_phase = wf.next_phase or "req_analyser"
                phase_to_agent = {
                    "req_analyser": "requirements_analyser",
                    "architecture_planner": "architecture_planner",
                }
                next_agent = phase_to_agent.get(next_phase, "requirements_analyser")
                wf.next_agent = next_agent
                wf.current_phase = cast(Any, next_phase)

                logger.warning(
                    "Workflow not complete; rerouting",
                    extra={
                        "missing_phase": next_phase,
                        "to_agent": next_agent,
                        "workflow_progress": wf.get_workflow_progress(),
                    },
                )

                reroute_msg = ToolMessage(
                    content=(
                        f"Architecture planning finished, but workflow is not complete. "
                        f"Routing to missing phase: {next_phase}."
                    ),
                    tool_call_id=runtime.tool_call_id,
                )
                return Command(
                    goto=next_agent,
                    update={
                        "active_agent": next_agent,
                        "current_step": next_phase,
                        "workflow_state": wf.model_dump(),
                        "status": "in_progress",
                        "messages": [last_ai, reroute_msg],
                        "handoff_data": runtime.state.get("handoff_data", {}),
                        "chart_plan": runtime.state.get("chart_plan", {}),
                        "files": runtime.state.get("files", {}),
                    },
                    graph=Command.PARENT,
                )

            # All phases complete — end the graph
            completion_msg = ToolMessage(
                content="Helm planning workflow complete.",
                tool_call_id=runtime.tool_call_id,
            )
            logger.info(
                "Workflow complete; ending graph",
                extra={"workflow_progress": wf.get_workflow_progress()},
            )
            return Command(
                goto=END,
                update={
                    "workflow_state": wf.model_dump(),
                    "status": "completed",
                    "messages": [last_ai, completion_msg],
                    "handoff_data": runtime.state.get("handoff_data", {}),
                    "chart_plan": runtime.state.get("chart_plan", {}),
                    "files": runtime.state.get("files", {}),
                },
                graph=Command.PARENT,
            )

        # ----------------------------------------------------------------
        # Build sub-agent graphs with handoff tools injected
        # ----------------------------------------------------------------

        self._req_analyser_agent = ReqAnalyserAgent(
            model=self.model,
            extra_tools=[transfer_to_architecture_planner],
        ).build_agent()

        self._arch_planner_agent = ArchitecturePlannerAgent(
            model=self.model,
            extra_tools=[complete_workflow],
        ).build_agent()

    # -- State transforms --------------------------------------------------

    def input_transform(self, send_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Transform the supervisor/coordinator payload into ``HelmPlannerState`` input.

        This is the **first** side of the planner's three-way bridge:
        1. ``input_transform``  — deep-agent state → HelmPlannerState
        2. ``planner_graph.invoke`` — runs the 2-phase planning pipeline
        3. ``output_transform`` — HelmPlannerState → deep-agent state update
        """
        messages = send_payload.get("messages") or []
        user_query = send_payload.get("user_query")

        if not user_query and messages:
            last = messages[-1]
            user_query = getattr(last, "content", None) or (
                last.get("content") if isinstance(last, dict) else None
            )

        wf_raw = send_payload.get("workflow_state")
        if isinstance(wf_raw, HelmPlannerWorkflowState):
            workflow_state = wf_raw
        elif isinstance(wf_raw, dict):
            workflow_state = HelmPlannerWorkflowState(**wf_raw)
        else:
            workflow_state = HelmPlannerWorkflowState()

        # Normalise to start of pipeline
        workflow_state.current_phase = "req_analyser"
        workflow_state.next_agent = "requirements_analyser"
        workflow_state.last_agent = None
        workflow_state.req_analyser_complete = False
        workflow_state.architecture_planner_complete = False
        workflow_state.workflow_complete = False

        transformed: Dict[str, Any] = {
            "messages": messages,
            "user_query": user_query or "",
            "session_id": send_payload.get("session_id"),
            "task_id": send_payload.get("task_id"),
            # Serialize to plain dict — LangGraph's msgpack checkpointer
            # cannot handle custom Pydantic models directly.
            "workflow_state": workflow_state.model_dump(),
            "status": "in_progress",
            "active_agent": "requirements_analyser",
            "current_step": "req_analyser",
            "files": send_payload.get("files", {}),
        }

        logger.info(
            "input_transform complete",
            extra={
                "user_query_preview": transformed["user_query"][:120],
                "session_id": transformed.get("session_id"),
                "task_id": transformed.get("task_id"),
                "workflow_progress": workflow_state.get_workflow_progress(),
            },
        )

        return transformed

    def output_transform(
        self,
        agent_state: Union[Dict[str, Any], Any],
        *,
        parent_files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Transform ``HelmPlannerState`` output → ``CompiledSubAgent`` return value.

        This is the **third** side of the planner's three-way bridge.

        ``CompiledSubAgent`` contract:
        - ``result["messages"][-1]``  MUST be an ``AIMessage`` — this is what
          the coordinator LLM sees as the subagent's single reply.
        - ``result["files"]``  (optional) is merged back into the deep-agent
          virtual FS.

        Args:
            agent_state: Final ``HelmPlannerState`` dict from
                         ``planner_graph.invoke()``.
            parent_files: Existing virtual-FS files from the deep-agent's
                          state, preserved alongside newly-generated files.

        Reference: PlannerSupervisorAgent.output_transform
        """
        if hasattr(agent_state, "model_dump"):
            agent_state = agent_state.model_dump()  # type: ignore[attr-defined]

        wf_raw = agent_state.get("workflow_state")
        if isinstance(wf_raw, HelmPlannerWorkflowState):
            wf = wf_raw
        elif isinstance(wf_raw, dict):
            wf = HelmPlannerWorkflowState(**wf_raw)
        else:
            wf = HelmPlannerWorkflowState()

        handoff_data = agent_state.get("handoff_data") or {}
        chart_plan = agent_state.get("chart_plan") or {}

        # ── Pass-through virtual FS files ─────────────────────────────────
        planner_files: Dict[str, Any] = agent_state.get("files") or {}
        merged_files: Dict[str, Any] = {**(parent_files or {}), **planner_files}

        # ── Build concise AIMessage summary for the coordinator ───────────
        summary_lines = ["## helm-planner: Planning complete"]

        if handoff_data:
            parsed = handoff_data.get("parsed_requirements", {})
            app_name = parsed.get("application_name", "")
            if app_name:
                summary_lines.append(f"\n**Requirements analysed** — Application: `{app_name}`")

            validation = handoff_data.get("validation_result", {})
            if validation:
                valid = validation.get("valid", True)
                summary_lines.append(f"  Validation: {'✅ passed' if valid else '❌ failed'}")

        if chart_plan:
            resources = chart_plan.get("resources_to_create", [])
            chart_name = chart_plan.get("chart_name", "")
            summary_lines.append(f"\n**Architecture plan** — Chart: `{chart_name}`")
            if resources:
                summary_lines.append(
                    "  Resources: " + ", ".join(f"`{r}`" for r in resources)
                )
            todos = chart_plan.get("generation_todos", [])
            if todos:
                summary_lines.append(
                    "  Generation TODOs: " + "; ".join(str(t) for t in todos[:5])
                )

        # List skill files the planner wrote
        skill_keys = sorted(
            k for k in merged_files if k.startswith("/skills") or k.startswith("skills")
        )
        if skill_keys:
            summary_lines.append(
                "\n**Skills written for downstream agents:**\n"
                + "\n".join(f"  - `{k}`" for k in skill_keys)
            )

        summary_text = "\n".join(summary_lines)

        logger.info(
            "output_transform: HelmPlannerState → CompiledSubAgent payload",
            extra={
                "workflow_complete": wf.is_complete,
                "workflow_progress": wf.get_workflow_progress(),
                "vfs_keys": list(merged_files.keys()),
                "skill_count": len(skill_keys),
                "summary_preview": summary_text[:200],
            },
        )

        return {
            "messages": [AIMessage(content=summary_text)],
            "files": merged_files,
        }

    # -- Graph builder -----------------------------------------------------

    def build_graph(self) -> Any:  # Returns CompiledStateGraph
        """Build the LangGraph subgraph with Command-based handoff routing.

        Uses ``add_conditional_edges`` with ``route_after_agent`` — identical
        to the reference pattern. Sub-agents use handoff tools to transfer
        control via ``Command(goto=..., graph=Command.PARENT)``.

        Reference: PlannerSupervisorAgent.build_graph
        """
        logger.info("Building HelmPlannerSupervisorAgent graph")

        def route_initial(state: HelmPlannerState) -> str:
            existing = state.get("workflow_state")
            wf: Optional[HelmPlannerWorkflowState] = None
            if isinstance(existing, dict):
                wf = HelmPlannerWorkflowState(**existing)
            elif isinstance(existing, HelmPlannerWorkflowState):
                wf = existing

            if wf and wf.next_agent:
                return wf.next_agent

            return cast(str, state.get("active_agent", "requirements_analyser"))

        def route_after_agent(state: HelmPlannerState) -> str:
            existing = state.get("workflow_state")
            wf: Optional[HelmPlannerWorkflowState] = None
            if isinstance(existing, dict):
                wf = HelmPlannerWorkflowState(**existing)
            elif isinstance(existing, HelmPlannerWorkflowState):
                wf = existing

            # End when workflow is complete
            if wf and wf.is_complete:
                return "__end__"

            # Safety net: if last message is AIMessage without tool_calls,
            # end the graph to prevent infinite loops.
            messages = state.get("messages", [])
            if messages:
                last_msg = messages[-1]
                if isinstance(last_msg, AIMessage) and not last_msg.tool_calls:
                    return "__end__"

            # Route to next agent
            if wf and wf.next_agent:
                return wf.next_agent

            return cast(str, state.get("active_agent", "requirements_analyser"))

        # Build the graph
        builder = StateGraph(HelmPlannerState)

        builder.add_node("requirements_analyser", self._req_analyser_agent)
        builder.add_node("architecture_planner", self._arch_planner_agent)

        # Entry routing
        builder.add_conditional_edges(
            START,
            route_initial,
            ["requirements_analyser", "architecture_planner"],
        )

        # Each agent can route to any other or END
        all_targets = [
            "requirements_analyser",
            "architecture_planner",
            END,
        ]
        for agent_node in ["requirements_analyser", "architecture_planner"]:
            builder.add_conditional_edges(agent_node, route_after_agent, all_targets)

        return builder.compile(checkpointer=self.memory)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_helm_planner_supervisor_agent(
    config: Optional[Any] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "helm_planner_supervisor",
    memory: Optional[MemorySaver] = None,
) -> HelmPlannerSupervisorAgent:
    """Create a HelmPlannerSupervisorAgent.

    Args:
        config: Configuration object.
        custom_config: Custom configuration dict.
        name: Agent name for routing.
        memory: MemorySaver checkpointer.

    Returns:
        HelmPlannerSupervisorAgent instance.
    """
    return HelmPlannerSupervisorAgent(
        config=config,
        custom_config=custom_config,
        name=name,
        memory=memory,
    )
