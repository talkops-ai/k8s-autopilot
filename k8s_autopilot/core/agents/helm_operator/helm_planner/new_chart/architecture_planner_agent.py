"""
Architecture Planner Agent — second sub-agent of the Helm planner pipeline.

Re-uses the existing analyzer tools from the planning swarm:
  - analyze_application_requirements (LLM chain → ApplicationAnalysisOutput)
  - design_kubernetes_architecture (LLM chain → KubernetesArchitectureOutput)
  - estimate_resources (LLM chain → ResourceEstimationOutput)
  - define_scaling_strategy (LLM chain → ScalingStrategyOutput)
  - check_dependencies (LLM chain → DependenciesOutput)

These tools are full implementations with Pydantic output schemas, prompt
templates, and LLM chains. They read from ``HelmPlannerState.handoff_data``
and write back via ``Command(update={...})``.

Pipeline: [ReqAnalyserAgent →] ArchitecturePlannerAgent

Reference: k8s_autopilot/core/agents/helm_generator/planner/planning_swarm.py
"""

from typing import List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langgraph.graph.state import CompiledStateGraph

from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.core.state.helm_planner_state import HelmPlannerState
from k8s_autopilot.core.agents.types import SubAgent

# Import the EXISTING production tools — no new tool definitions here
from k8s_autopilot.core.agents.helm_operator.helm_planner.new_chart.arch_planner_tool.arch_planner_tool import (
    analyze_application_requirements,
    design_kubernetes_architecture,
    estimate_resources,
    define_scaling_strategy,
    check_dependencies,
)
from k8s_autopilot.core.agents.helm_operator.helm_planner.new_chart.arch_planner_tool.skill_writer_tool import (
    write_chart_skills_tool,
)
ARCHITECTURE_PLANNER_SUBAGENT_PROMPT = """
You are a Kubernetes architecture expert specializing in Helm chart design.

## Your Responsibilities

Design production-ready Kubernetes architectures for Helm charts following Bitnami standards.

## TOOLS (Use in order)

1. **analyze_application_requirements**: Deep analysis of framework, language, and runtime characteristics
   - Startup time, memory footprint, CPU needs
   - Connection pooling requirements
   - Graceful shutdown periods

2. **design_kubernetes_architecture**: Plan K8s resources structure
   - Which resources to create (Deployment, StatefulSet, DaemonSet, etc.)
   - Service topology and exposure strategy
   - ConfigMap/Secret management
   - Storage requirements (PVC, emptyDir, etc.)

3. **estimate_resources**: Calculate CPU/memory requests and limits
   - Based on app framework characteristics
   - Scaling behavior analysis
   - Resource optimization recommendations

4. **define_scaling_strategy**: Design HPA configuration
   - Horizontal scaling parameters
   - Metric targets (CPU, memory, custom)
   - Scale-up/down policies

5. **check_dependencies**: Identify required charts and external services
   - Database charts (PostgreSQL, MySQL, Redis)
   - Message queues (RabbitMQ, Kafka)
   - Other dependencies

6. **write_chart_skills_tool**: Compile all generated planning outputs into the virtual filesystem
   - Generates SKILL.md and reference blueprints
   - MUST be called before ending the workflow

7. **complete_workflow**: CRITICAL MANDATORY STEP.
   You MUST call this tool as your final action to end the planning workflow and hand off control back to the deep agent coordinator.
   Do NOT ask the user if they want to proceed, just call the tool.

## WORKFLOW

1. Start with analyze_application_requirements for deep app understanding
2. Use design_kubernetes_architecture to plan resource structure
3. Call estimate_resources for sizing recommendations
4. Define scaling with define_scaling_strategy
5. Check dependencies with check_dependencies
6. Call write_chart_skills_tool to compile the plan.
7. Call complete_workflow as the final mandatory step.

## OUTPUT FORMAT

Provide structured architecture recommendations including:

### Kubernetes Resources
- List of resources to create with justification
- Service type and exposure strategy
- Storage strategy

### Resource Sizing
- CPU requests/limits with reasoning
- Memory requests/limits with reasoning
- Storage size if applicable

### Scaling Configuration
- HPA settings (min/max replicas, metrics)
- Scaling behavior recommendations

### Dependencies
- Required Helm chart dependencies
- External service requirements
- Integration points

### Best Practices Applied
- Bitnami compliance checklist
- Security hardening recommendations
- High availability considerations
"""

logger = AgentLogger("ArchitecturePlannerAgent")


class ArchitecturePlannerAgent(SubAgent):
    """Agent that orchestrates the architecture planning pipeline.

    Wraps the existing analyzer tools (analyze_application_requirements,
    design_kubernetes_architecture, estimate_resources, define_scaling_strategy,
    check_dependencies) into the ``SubAgent`` contract so it can be mounted as
    a node in the ``HelmPlannerSupervisorAgent`` StateGraph.

    The tools themselves contain full LLM chain implementations with Pydantic
    schemas and prompt templates — this agent only provides the graph wrapper.

    Reference: k8s_autopilot planning_swarm.py architecture_planner_agent
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
        return "architecture_planner"

    @property
    def description(self) -> str:
        return ARCHITECTURE_PLANNER_SUBAGENT_PROMPT

    def get_tools(self) -> List[BaseTool]:
        """Return the existing analyzer tools + any handoff tools.

        Uses the pre-built tools from:
            k8s_autopilot.core.agents.helm_generator.planner.tools.analyzer
        """
        base_tools: List[BaseTool] = [
            analyze_application_requirements,
            design_kubernetes_architecture,
            estimate_resources,
            define_scaling_strategy,
            check_dependencies,
            write_chart_skills_tool,
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
        )
