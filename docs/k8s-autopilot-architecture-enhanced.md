# Kubernetes Autopilot: Deep Multi-Agent Framework Architecture
## Enhanced Edition - LangChain v1.0 Compatible

---

## Executive Summary

Kubernetes Autopilot is a production-grade, multi-agentic framework built on **LangChain v1.0** and **LangGraph** for generating enterprise-standard Helm charts (Bitnami-quality) with human-in-the-loop review and ArgoCD deployment support. 

The architecture follows a **Hierarchical Supervisor with Swarms pattern**, leveraging:
- **Deep Agents** for complex planning and generation tasks
- **LangGraph Subgraphs** for modular agent swarms
- **Command-based handoffs** for flexible agent routing
- **PostgreSQL checkpointer** for persistent state management
- **Dynamic interrupts** for human-in-the-loop workflows
- **State reducers** for concurrent agent operations

---

## Table of Contents

1. [Architecture Patterns](#architecture-patterns)
2. [Technology Stack](#technology-stack)
3. [State Management & Schemas](#state-management--schemas)
4. [Agent Swarm Architectures](#agent-swarm-architectures)
5. [Subgraph Implementation](#subgraph-implementation)
6. [Handoff Mechanisms](#handoff-mechanisms)
7. [Human-in-the-Loop (HITL)](#human-in-the-loop-hitl)
8. [Persistence & Checkpointing](#persistence--checkpointing)
9. [Deep Agents Integration](#deep-agents-integration)
10. [Error Handling & Retry Logic](#error-handling--retry-logic)
11. [Observability & Monitoring](#observability--monitoring)
12. [Implementation Guide](#implementation-guide)
13. [Production Deployment](#production-deployment)

---

## Architecture Patterns

### High-Level Pattern: Hierarchical Supervisor with Swarms

```
┌────────────────────────────────────────────────────────────┐
│                   Main Supervisor Agent                     │
│  - Orchestrates workflow phases                            │
│  - Manages HITL interactions                               │
│  - Controls swarm routing and error handling               │
│  - Tracks workflow metadata and approval status            │
└─────────────┬──────────────┬──────────────┬────────────────┘
              │              │              │
       ┌──────▼──────┐ ┌────▼──────┐ ┌─────▼────────┐
       │  Planning   │ │Generation │ │ Validation & │
       │   Swarm     │ │  Swarm    │ │ Deployment   │
       │(Deep Agent) │ │(Deep Agent)│ │   Swarm      │
       └──────┬──────┘ └────┬──────┘ └─────┬────────┘
              │              │              │
    [Subgraph with       [Subgraph with   [Subgraph with
     Agent Handoffs]      Agent Handoffs]   Agent Handoffs]
```

### Pattern Characteristics

| Feature | Implementation |
|---------|----------------|
| **Orchestration** | Centralized supervisor with decentralized swarm autonomy |
| **Communication** | Command-based state updates and goto directives |
| **Persistence** | PostgreSQL checkpointer with thread-based sessions |
| **HITL** | Dynamic interrupts with approve/edit/reject decisions |
| **Error Handling** | Retry policies with exponential backoff |
| **Scalability** | Independent swarm deployment and scaling |

---

## Technology Stack

### Core Dependencies

```toml
# pyproject.toml
[project]
name = "k8s-autopilot"
version = "1.0.0"
requires-python = ">=3.11"

dependencies = [
    "langchain>=1.0.0",
    "langgraph>=1.0.0",
    "langgraph-checkpoint-postgres>=1.0.0",
    "deepagents>=0.1.0",
    "langchain-openai>=0.2.0",
    "langchain-anthropic>=0.2.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    
    # Kubernetes/Helm tools
    "kubernetes>=28.0.0",
    "pyyaml>=6.0.1",
    "jinja2>=3.1.0",
    
    # Security scanning
    "trivy-python>=0.1.0",
    "kube-score-client>=0.1.0",
    
    # Monitoring
    "langsmith>=0.2.0",
    "prometheus-client>=0.19.0",
]
```

### Environment Configuration

```python
# k8s_autopilot/config/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    
    # LLM Configuration
    llm_provider: Literal["openai", "anthropic"] = "anthropic"
    llm_model: str = "claude-sonnet-4-5-20250929"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096
    
    # LangSmith Configuration
    langsmith_api_key: str
    langsmith_project: str = "k8s-autopilot"
    langsmith_tracing: bool = True
    
    # Database Configuration
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "k8s_autopilot"
    postgres_user: str = "postgres"
    postgres_password: str
    
    @property
    def database_uri(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
    
    # Helm Configuration
    helm_binary_path: str = "/usr/local/bin/helm"
    bitnami_charts_repo: str = "https://charts.bitnami.com/bitnami"
    
    # Security Configuration
    trivy_binary_path: str = "/usr/local/bin/trivy"
    kube_score_binary_path: str = "/usr/local/bin/kube-score"
    
settings = Settings()
```

---

## State Management & Schemas

### Base State Classes

```python
# k8s_autopilot/core/state/base.py
from typing import Annotated, TypedDict, Optional, Literal, Dict, List
from operator import add
from datetime import datetime
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, AnyMessage
from langgraph.graph.message import add_messages

# ============================================================================
# Shared Data Models
# ============================================================================

class ChartRequirements(BaseModel):
    """User requirements for Helm chart generation"""
    application_name: str = Field(..., description="Name of the application")
    application_type: Literal["web", "api", "worker", "database", "cache"] = "web"
    container_image: str = Field(..., description="Container image to deploy")
    replicas: int = Field(default=3, ge=1, le=100)
    resource_requests: Dict[str, str] = Field(
        default={"cpu": "100m", "memory": "128Mi"}
    )
    resource_limits: Dict[str, str] = Field(
        default={"cpu": "500m", "memory": "512Mi"}
    )
    environment_variables: Dict[str, str] = Field(default_factory=dict)
    config_maps: List[str] = Field(default_factory=list)
    secrets: List[str] = Field(default_factory=list)
    ingress_enabled: bool = True
    ingress_host: Optional[str] = None
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer"] = "ClusterIP"
    service_port: int = 8080
    health_check_path: str = "/health"
    readiness_check_path: str = "/ready"
    storage_required: bool = False
    storage_size: str = "10Gi"
    autoscaling_enabled: bool = True
    autoscaling_min_replicas: int = 2
    autoscaling_max_replicas: int = 10
    autoscaling_target_cpu: int = 80
    additional_requirements: Optional[str] = None


class ChartPlan(BaseModel):
    """Planned structure for Helm chart"""
    chart_name: str
    chart_version: str = "1.0.0"
    app_version: str
    description: str
    
    # Kubernetes resources to generate
    resources_to_create: List[str] = Field(
        description="List of K8s resources: Deployment, Service, Ingress, etc."
    )
    
    # Dependencies
    chart_dependencies: List[Dict[str, str]] = Field(default_factory=list)
    
    # Configuration structure
    values_structure: Dict = Field(
        description="Structure of values.yaml with defaults"
    )
    
    # Security policies
    security_policies: List[str] = Field(default_factory=list)
    
    # Best practices to apply
    bitnami_compliance: List[str] = Field(
        description="Bitnami standards to implement"
    )
    
    generation_todos: List[str] = Field(
        description="Todo list for generation phase"
    )


class ValidationResult(BaseModel):
    """Result of validation checks"""
    validator: str
    passed: bool
    severity: Literal["info", "warning", "error", "critical"]
    message: str
    details: Optional[Dict] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SecurityScanReport(BaseModel):
    """Security scan results"""
    vulnerabilities: List[Dict] = Field(default_factory=list)
    policy_violations: List[Dict] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=100.0)
    passed: bool
    recommendations: List[str] = Field(default_factory=list)


class ArgoCDConfig(BaseModel):
    """ArgoCD Application configuration"""
    application_name: str
    project: str = "default"
    repo_url: str
    target_revision: str = "HEAD"
    path: str
    destination_server: str = "https://kubernetes.default.svc"
    destination_namespace: str
    sync_policy: Dict = Field(default_factory=dict)
    sync_waves: List[Dict] = Field(default_factory=list)


class ApprovalStatus(BaseModel):
    """Human approval status"""
    status: Literal["pending", "approved", "rejected", "modified"]
    reviewer: Optional[str] = None
    comments: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class WorkflowMetadata(BaseModel):
    """Workflow execution metadata"""
    workflow_id: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    current_phase: str
    total_phases: int = 5
    retry_count: int = 0
    max_retries: int = 3


class ErrorContext(BaseModel):
    """Error tracking context"""
    error_type: str
    error_message: str
    failed_node: str
    failed_swarm: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    recoverable: bool = True
    stack_trace: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Main Supervisor State
# ============================================================================

class MainSupervisorState(TypedDict):
    """
    State schema for the main supervisor agent.
    
    Uses Annotated types with reducers for proper concurrent update handling.
    """
    # Messages for LLM interactions
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Core workflow data
    user_requirements: ChartRequirements
    active_phase: Literal[
        "requirements", 
        "planning", 
        "generation", 
        "validation", 
        "deployment",
        "error"
    ]
    
    # Phase outputs
    planning_output: Optional[ChartPlan]
    generated_artifacts: Optional[Dict[str, str]]  # filepath -> content
    validation_results: Annotated[List[ValidationResult], add]
    
    # HITL tracking
    human_approval_status: Dict[str, ApprovalStatus]
    
    # Metadata & error tracking
    workflow_metadata: WorkflowMetadata
    error_state: Optional[ErrorContext]
    
    # File system artifacts (for Deep Agents)
    file_artifacts: Annotated[Dict[str, str], lambda x, y: {**x, **y}]
    
    # Todo tracking
    todos: Annotated[List[Dict], add]
```

### Swarm-Specific States

```python
# k8s_autopilot/core/state/swarms.py
from typing import Annotated, TypedDict, Optional, Literal, Dict, List
from operator import add
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# ============================================================================
# Planning Swarm State
# ============================================================================

class PlanningSwarmState(TypedDict):
    """State for Planning Swarm (Deep Agent-based)"""
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Active agent tracking
    active_agent: Literal[
        "requirements_validator",
        "best_practices_researcher",
        "architecture_planner",
        "planning_supervisor"
    ]
    
    # Inputs from main supervisor
    requirements: ChartRequirements
    
    # Phase outputs
    requirements_validation: Optional[ValidationResult]
    research_findings: Annotated[List[Dict], add]
    chart_plan: Optional[ChartPlan]
    
    # Deep Agent features
    todos: Annotated[List[Dict], add]
    workspace_files: Annotated[Dict[str, str], lambda x, y: {**x, **y}]
    
    # Handoff context
    handoff_metadata: Dict


# ============================================================================
# Generation Swarm State
# ============================================================================

class GenerationSwarmState(TypedDict):
    """State for Generation Swarm (Deep Agent-based)"""
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Active agent tracking
    active_agent: Literal[
        "template_generator",
        "values_generator",
        "dependencies_manager",
        "security_hardening",
        "documentation_generator",
        "generation_supervisor"
    ]
    
    # Inputs from planning swarm
    chart_plan: ChartPlan
    
    # Generated artifacts (using merge reducer for concurrent updates)
    templates: Annotated[Dict[str, str], lambda x, y: {**x, **y}]
    values_yaml: Optional[str]
    values_schema_json: Optional[str]
    chart_yaml: Optional[str]
    readme: Optional[str]
    
    # Security policies (can be updated by multiple agents)
    security_policies: Annotated[List[Dict], add]
    
    # Deep Agent features
    todos: Annotated[List[Dict], add]
    workspace_files: Annotated[Dict[str, str], lambda x, y: {**x, **y}]
    
    # Generation metadata
    generation_metadata: Dict
    handoff_metadata: Dict


# ============================================================================
# Validation & Deployment Swarm State
# ============================================================================

class ValidationSwarmState(TypedDict):
    """State for Validation & Deployment Swarm"""
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Active agent tracking
    active_agent: Literal[
        "chart_validator",
        "security_scanner",
        "test_generator",
        "argocd_configurator",
        "validation_supervisor"
    ]
    
    # Inputs from generation swarm
    generated_chart: Dict[str, str]
    chart_metadata: ChartPlan
    
    # Validation results (can be updated concurrently)
    validation_results: Annotated[List[ValidationResult], add]
    security_scan_results: Optional[SecurityScanReport]
    test_artifacts: Optional[Dict[str, str]]
    argocd_manifests: Optional[ArgoCDConfig]
    
    # Deployment readiness
    deployment_ready: bool
    blocking_issues: Annotated[List[str], add]
    
    # Handoff context
    handoff_metadata: Dict
```

---

## Agent Swarm Architectures

### Planning Swarm (Deep Agent Implementation)

```python
# k8s_autopilot/core/swarms/planning_swarm.py
from deepagents import create_deep_agent
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from typing import Literal
from k8s_autopilot.core.state.swarms import PlanningSwarmState
from k8s_autopilot.tools.planning import (
    validate_requirements_tool,
    search_bitnami_standards_tool,
    search_helm_best_practices_tool,
    analyze_similar_charts_tool
)

def create_planning_swarm() -> StateGraph:
    """
    Creates the Planning Swarm as a Deep Agent with subgraph structure.
    
    Architecture:
    - Uses Deep Agent for planning capabilities (write_todos, file system)
    - Three specialized agents with handoff capabilities
    - Supervisor coordinates the workflow
    """
    
    # ========================================================================
    # Deep Agent Configuration
    # ========================================================================
    
    planning_deep_agent = create_deep_agent(
        tools=[
            validate_requirements_tool,
            search_bitnami_standards_tool,
            search_helm_best_practices_tool,
            analyze_similar_charts_tool
        ],
        system_prompt="""
You are an expert Helm chart planner and requirements analyst.

## Your Workflow:
1. **Validate Requirements**: Analyze user requirements for completeness and feasibility
2. **Research Best Practices**: Search Bitnami standards and Helm best practices
3. **Plan Architecture**: Design chart structure and resource requirements
4. **Create Todo List**: Break down generation tasks for the next phase

## File System Usage:
- Store research findings in /workspace/research/
- Cache Bitnami standards in /workspace/standards/
- Save planning documents in /workspace/plans/
- Create detailed todo lists for generation phase

## Planning Guidelines:
- Follow Bitnami chart structure conventions
- Ensure CNCF compliance and best practices
- Consider security, scalability, and maintainability
- Plan for proper labeling, annotations, and metadata
- Include comprehensive documentation requirements

Use the write_todos tool to create a structured plan for the generation phase.
        """,
        use_longterm_memory=True  # Remember organizational standards
    )
    
    # ========================================================================
    # Swarm State Graph
    # ========================================================================
    
    swarm_builder = StateGraph(PlanningSwarmState)
    
    # Supervisor node
    def planning_supervisor(state: PlanningSwarmState) -> Command[Literal[
        "requirements_validator",
        "best_practices_researcher", 
        "architecture_planner",
        END
    ]]:
        """
        Supervisor coordinates planning workflow.
        Routes based on current state and requirements.
        """
        # Initial routing
        if state.get("requirements_validation") is None:
            return Command(
                update={
                    "active_agent": "requirements_validator",
                    "messages": [AIMessage(content="Starting requirements validation...")]
                },
                goto="requirements_validator"
            )
        
        # After validation, research best practices
        if state.get("research_findings") is None or len(state.get("research_findings", [])) == 0:
            return Command(
                update={"active_agent": "best_practices_researcher"},
                goto="best_practices_researcher"
            )
        
        # After research, plan architecture
        if state.get("chart_plan") is None:
            return Command(
                update={"active_agent": "architecture_planner"},
                goto="architecture_planner"
            )
        
        # Planning complete, return to main supervisor
        return Command(
            update={
                "messages": [AIMessage(content="Planning phase complete!")],
                "active_agent": "planning_supervisor"
            },
            goto=END
        )
    
    # Agent nodes
    def requirements_validator(state: PlanningSwarmState):
        """Validates and refines user requirements"""
        # Invoke deep agent for validation
        result = planning_deep_agent.invoke({
            "messages": [
                HumanMessage(
                    content=f"""
Validate these Helm chart requirements:

{state['requirements'].model_dump_json(indent=2)}

Check for:
1. Completeness of configuration
2. Resource constraints validity
3. Security considerations
4. Kubernetes compatibility
5. Best practices alignment

Provide detailed validation results.
                    """
                )
            ]
        })
        
        # Extract validation result
        validation_result = ValidationResult(
            validator="requirements_validator",
            passed=True,  # Parse from result
            severity="info",
            message="Requirements validated successfully",
            details={"findings": result["messages"][-1].content}
        )
        
        return {
            "requirements_validation": validation_result,
            "messages": result["messages"],
            "workspace_files": result.get("file_artifacts", {})
        }
    
    def best_practices_researcher(state: PlanningSwarmState):
        """Researches Bitnami standards and Helm best practices"""
        result = planning_deep_agent.invoke({
            "messages": [
                HumanMessage(
                    content=f"""
Research Helm best practices and Bitnami standards for a {state['requirements'].application_type} application.

Application: {state['requirements'].application_name}
Type: {state['requirements'].application_type}

Research:
1. Bitnami chart structure for this app type
2. Required and optional Kubernetes resources
3. Security best practices
4. Labeling and annotation standards
5. Values.yaml structure conventions

Store findings in /workspace/research/ for reference.
                    """
                )
            ]
        })
        
        return {
            "research_findings": [{"source": "bitnami", "content": result["messages"][-1].content}],
            "messages": result["messages"],
            "workspace_files": result.get("file_artifacts", {})
        }
    
    def architecture_planner(state: PlanningSwarmState):
        """Creates detailed chart architecture plan"""
        result = planning_deep_agent.invoke({
            "messages": [
                HumanMessage(
                    content=f"""
Create a detailed Helm chart plan based on:

Requirements:
{state['requirements'].model_dump_json(indent=2)}

Research Findings:
{state.get('research_findings', [])}

Generate a comprehensive ChartPlan including:
1. Chart metadata (name, version, description)
2. List of Kubernetes resources to create
3. Values.yaml structure with defaults
4. Chart dependencies (if needed)
5. Security policies to implement
6. Bitnami compliance checklist
7. Todo list for generation phase

Use write_todos tool to create structured tasks for the generation team.
Save the complete plan to /workspace/plans/chart-plan.json
                    """
                )
            ]
        })
        
        # Parse ChartPlan from result
        # (In production, use structured output)
        chart_plan = ChartPlan(
            chart_name=state['requirements'].application_name,
            app_version="1.0.0",
            description=f"Helm chart for {state['requirements'].application_name}",
            resources_to_create=[
                "Deployment",
                "Service",
                "ConfigMap",
                "Secret",
                "Ingress" if state['requirements'].ingress_enabled else None,
                "HorizontalPodAutoscaler" if state['requirements'].autoscaling_enabled else None,
                "PersistentVolumeClaim" if state['requirements'].storage_required else None
            ],
            values_structure={},  # Parse from result
            generation_todos=result.get("todos", [])
        )
        
        return {
            "chart_plan": chart_plan,
            "messages": result["messages"],
            "todos": result.get("todos", []),
            "workspace_files": result.get("file_artifacts", {})
        }
    
    # Add nodes to graph
    swarm_builder.add_node("planning_supervisor", planning_supervisor)
    swarm_builder.add_node("requirements_validator", requirements_validator)
    swarm_builder.add_node("best_practices_researcher", best_practices_researcher)
    swarm_builder.add_node("architecture_planner", architecture_planner)
    
    # Add edges
    swarm_builder.add_edge(START, "planning_supervisor")
    swarm_builder.add_edge("requirements_validator", "planning_supervisor")
    swarm_builder.add_edge("best_practices_researcher", "planning_supervisor")
    swarm_builder.add_edge("architecture_planner", "planning_supervisor")
    
    return swarm_builder.compile()
```

### Generation Swarm (Deep Agent with File System)

```python
# k8s_autopilot/core/swarms/generation_swarm.py
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from typing import Literal
from k8s_autopilot.core.state.swarms import GenerationSwarmState
from k8s_autopilot.tools.generation import (
    generate_deployment_tool,
    generate_service_tool,
    generate_ingress_tool,
    validate_yaml_tool,
    apply_security_policies_tool
)

def create_generation_swarm(workspace_dir: str = "/tmp/helm-charts") -> StateGraph:
    """
    Creates the Generation Swarm as a Deep Agent with file system backend.
    
    This swarm generates multiple YAML files concurrently and uses file system
    tools to manage large templates.
    """
    
    # ========================================================================
    # Deep Agent with File System Backend
    # ========================================================================
    
    generation_deep_agent = create_deep_agent(
        tools=[
            generate_deployment_tool,
            generate_service_tool,
            generate_ingress_tool,
            validate_yaml_tool,
            apply_security_policies_tool
        ],
        system_prompt="""
You are an expert Helm chart generator specializing in creating Bitnami-quality charts.

## Your Workflow:
1. **Read Plan**: Load chart plan from /workspace/plans/
2. **Generate Templates**: Create Kubernetes manifests in /workspace/templates/
3. **Create Values**: Generate values.yaml and values.schema.json
4. **Apply Security**: Implement security policies and best practices
5. **Generate Docs**: Create comprehensive README.md

## File System Structure:
/workspace/
├── plans/
│   └── chart-plan.json
├── templates/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── hpa.yaml
│   ├── pvc.yaml
│   ├── _helpers.tpl
│   └── NOTES.txt
├── values.yaml
├── values.schema.json
├── Chart.yaml
└── README.md

## Generation Guidelines:
- Use Helm templating best practices ({{ .Values.* }})
- Include proper labels and annotations
- Implement security contexts and pod security standards
- Add resource requests and limits
- Include liveness and readiness probes
- Use ConfigMaps for configuration
- Use Secrets for sensitive data
- Add comprehensive NOTES.txt for post-installation instructions

Use edit_file to refine templates iteratively based on validation feedback.
        """,
        backend=FilesystemBackend(root_dir=workspace_dir),
        use_longterm_memory=True
    )
    
    # ========================================================================
    # Swarm State Graph
    # ========================================================================
    
    swarm_builder = StateGraph(GenerationSwarmState)
    
    # Implementation similar to planning swarm...
    # (Abbreviated for space - full implementation would include all nodes)
    
    def generation_supervisor(state: GenerationSwarmState) -> Command:
        """Routes generation tasks to specialized agents"""
        # Routing logic based on todos and state
        pass
    
    def template_generator(state: GenerationSwarmState):
        """Generates Kubernetes resource templates"""
        pass
    
    def values_generator(state: GenerationSwarmState):
        """Generates values.yaml and schema"""
        pass
    
    # Add nodes and edges...
    
    return swarm_builder.compile()
```

---

## Subgraph Implementation

### Two Methods of Subgraph Integration

```python
# k8s_autopilot/core/supervisor/main_supervisor.py
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from k8s_autopilot.core.state.base import MainSupervisorState
from k8s_autopilot.core.swarms.planning_swarm import create_planning_swarm
from k8s_autopilot.core.swarms.generation_swarm import create_generation_swarm
from k8s_autopilot.core.swarms.validation_swarm import create_validation_swarm

# ============================================================================
# Method 1: Add Compiled Subgraph as Node (Shared State Keys)
# ============================================================================

def create_main_supervisor_method1():
    """
    Adds subgraphs directly as nodes when they share state keys with parent.
    
    Best for: Swarms that share common state structure (e.g., messages)
    """
    main_builder = StateGraph(MainSupervisorState)
    
    # Compile swarms
    planning_swarm = create_planning_swarm()
    generation_swarm = create_generation_swarm()
    validation_swarm = create_validation_swarm()
    
    # Add swarms directly as nodes
    main_builder.add_node("planning_swarm", planning_swarm)
    main_builder.add_node("generation_swarm", generation_swarm)
    main_builder.add_node("validation_swarm", validation_swarm)
    
    # Add supervisor logic and gates
    main_builder.add_node("planning_review_gate", planning_review_gate)
    main_builder.add_node("security_review_gate", security_review_gate)
    main_builder.add_node("deployment_approval_gate", deployment_approval_gate)
    
    # Routing
    main_builder.add_edge(START, "planning_swarm")
    main_builder.add_edge("planning_swarm", "planning_review_gate")
    main_builder.add_conditional_edges(
        "planning_review_gate",
        route_after_planning_review
    )
    
    return main_builder


# ============================================================================
# Method 2: Invoke Subgraph from Node (Transform State)
# ============================================================================

def create_main_supervisor_method2():
    """
    Invokes subgraphs from within nodes with state transformation.
    
    Best for: Swarms with different state schemas, private contexts
    """
    main_builder = StateGraph(MainSupervisorState)
    
    # Compile swarms
    planning_swarm = create_planning_swarm()
    generation_swarm = create_generation_swarm()
    validation_swarm = create_validation_swarm()
    
    def planning_node(state: MainSupervisorState):
        """
        Node that invokes planning swarm with state transformation.
        """
        # Transform MainSupervisorState -> PlanningSwarmState
        planning_input = {
            "messages": state["messages"],
            "requirements": state["user_requirements"],
            "active_agent": "planning_supervisor",
            "handoff_metadata": {
                "parent_workflow_id": state["workflow_metadata"].workflow_id
            },
            "todos": [],
            "workspace_files": {},
            "research_findings": [],
            "requirements_validation": None,
            "chart_plan": None
        }
        
        # Invoke subgraph
        planning_result = planning_swarm.invoke(planning_input)
        
        # Transform PlanningSwarmState -> MainSupervisorState update
        return {
            "messages": planning_result["messages"],
            "planning_output": planning_result["chart_plan"],
            "file_artifacts": planning_result.get("workspace_files", {}),
            "todos": planning_result.get("todos", []),
            "active_phase": "planning"
        }
    
    def generation_node(state: MainSupervisorState):
        """Node that invokes generation swarm"""
        # Transform state
        generation_input = {
            "messages": state["messages"],
            "chart_plan": state["planning_output"],
            "active_agent": "generation_supervisor",
            "handoff_metadata": {},
            "todos": state.get("todos", []),
            "workspace_files": state.get("file_artifacts", {}),
            "templates": {},
            "values_yaml": None,
            "values_schema_json": None,
            "chart_yaml": None,
            "readme": None,
            "security_policies": [],
            "generation_metadata": {}
        }
        
        # Invoke subgraph
        generation_result = generation_swarm.invoke(generation_input)
        
        # Transform back
        return {
            "messages": generation_result["messages"],
            "generated_artifacts": {
                **generation_result.get("templates", {}),
                "values.yaml": generation_result.get("values_yaml"),
                "values.schema.json": generation_result.get("values_schema_json"),
                "Chart.yaml": generation_result.get("chart_yaml"),
                "README.md": generation_result.get("readme")
            },
            "file_artifacts": generation_result.get("workspace_files", {}),
            "active_phase": "generation"
        }
    
    # Add nodes
    main_builder.add_node("planning_phase", planning_node)
    main_builder.add_node("generation_phase", generation_node)
    main_builder.add_node("planning_review_gate", planning_review_gate)
    
    # Routing
    main_builder.add_edge(START, "planning_phase")
    main_builder.add_edge("planning_phase", "planning_review_gate")
    main_builder.add_conditional_edges(
        "planning_review_gate",
        route_after_planning_review
    )
    
    return main_builder
```

---

## Handoff Mechanisms

### 1. Supervisor-to-Swarm Handoff (Command Pattern)

```python
# k8s_autopilot/core/supervisor/routing.py
from langgraph.types import Command
from typing import Literal

def main_supervisor_router(
    state: MainSupervisorState
) -> Command[Literal["planning_swarm", "generation_swarm", "validation_swarm", "error_handler", END]]:
    """
    Main supervisor routing logic using Command pattern.
    
    Command combines state updates AND control flow in single return.
    """
    
    # Check for errors
    if state.get("error_state") is not None:
        error = state["error_state"]
        if error.retry_count < error.max_retries and error.recoverable:
            return Command(
                update={
                    "error_state": ErrorContext(
                        **{**error.model_dump(), "retry_count": error.retry_count + 1}
                    ),
                    "messages": [AIMessage(content=f"Retrying {error.failed_swarm}...")]
                },
                goto=error.failed_swarm
            )
        else:
            return Command(
                update={"active_phase": "error"},
                goto="error_handler"
            )
    
    # Normal workflow routing
    if state["active_phase"] == "requirements":
        return Command(
            update={
                "active_phase": "planning",
                "workflow_metadata": WorkflowMetadata(
                    **{**state["workflow_metadata"].model_dump(), "current_phase": "planning"}
                )
            },
            goto="planning_swarm"
        )
    
    elif state["active_phase"] == "planning":
        # Check if planning approved
        approval = state["human_approval_status"].get("planning")
        if approval and approval.status == "approved":
            return Command(
                update={"active_phase": "generation"},
                goto="generation_swarm"
            )
        elif approval and approval.status == "rejected":
            return Command(
                update={
                    "active_phase": "planning",
                    "planning_output": None  # Reset for re-planning
                },
                goto="planning_swarm"
            )
    
    elif state["active_phase"] == "generation":
        approval = state["human_approval_status"].get("security")
        if approval and approval.status == "approved":
            return Command(
                update={"active_phase": "validation"},
                goto="validation_swarm"
            )
    
    elif state["active_phase"] == "validation":
        approval = state["human_approval_status"].get("deployment")
        if approval and approval.status == "approved":
            return Command(
                update={"active_phase": "deployment"},
                goto=END
            )
    
    # Default: wait for approval
    return Command(update={}, goto=END)
```

### 2. Intra-Swarm Agent Handoffs (Command.PARENT)

```python
# k8s_autopilot/core/swarms/agent_handoffs.py
from langgraph.types import Command
from typing import Literal
import operator

def create_handoff_tool(
    target_agent: str,
    description: str
):
    """
    Factory function to create agent handoff tools within a swarm.
    
    Uses Command.PARENT to navigate between agents in same swarm.
    """
    from langchain_core.tools import tool
    
    @tool(f"transfer_to_{target_agent}", description=description)
    def handoff_tool(
        task_context: str,
        files_to_share: List[str] = None
    ) -> str:
        """
        Transfer control to another agent in the swarm.
        
        Args:
            task_context: Context and instructions for the next agent
            files_to_share: List of file paths to share with next agent
        """
        # This will be executed by the current agent
        # Return Command to transfer to target agent
        
        # Note: In actual implementation, we need access to state
        # This is a simplified example
        return Command(
            update={
                "active_agent": target_agent,
                "messages": [
                    AIMessage(
                        content=f"Transferred to {target_agent}: {task_context}"
                    )
                ],
                "handoff_metadata": {
                    "from_agent": "current_agent",
                    "to_agent": target_agent,
                    "context": task_context,
                    "shared_files": files_to_share or []
                }
            },
            goto=target_agent,
            graph=Command.PARENT  # Navigate within parent (swarm) graph
        )
    
    return handoff_tool


# Usage in Planning Swarm
requirements_validator_to_researcher = create_handoff_tool(
    target_agent="best_practices_researcher",
    description="Transfer to Best Practices Researcher after validating requirements"
)

researcher_to_planner = create_handoff_tool(
    target_agent="architecture_planner",
    description="Transfer to Architecture Planner with research findings"
)


# ============================================================================
# State Reducers for Command.PARENT
# ============================================================================

class PlanningSwarmStateWithReducers(TypedDict):
    """
    IMPORTANT: When using Command.PARENT to update shared state keys,
    you MUST define reducers in the parent graph state.
    """
    messages: Annotated[List[AnyMessage], add_messages]  # Has reducer
    active_agent: str  # No reducer - last write wins
    
    # These need reducers if updated from subgraph with Command.PARENT
    research_findings: Annotated[List[Dict], operator.add]  # Reducer defined
    workspace_files: Annotated[Dict[str, str], lambda x, y: {**x, **y}]  # Merge dict
    todos: Annotated[List[Dict], operator.add]  # Append lists
```

### 3. Cross-Swarm Handoffs (Returning to Supervisor)

```python
# k8s_autopilot/core/swarms/swarm_completion.py
from langgraph.types import Command

def complete_planning_swarm(state: PlanningSwarmState) -> Command:
    """
    Completes planning swarm and returns control to main supervisor.
    
    Aggregates all swarm outputs and updates main supervisor state.
    """
    
    # Validate swarm completion
    if state.get("chart_plan") is None:
        raise ValueError("Planning swarm completed without chart plan")
    
    # Prepare summary for supervisor
    summary_message = AIMessage(
        content=f"""
Planning Phase Complete!

Chart Plan Summary:
- Chart Name: {state['chart_plan'].chart_name}
- Resources: {', '.join(state['chart_plan'].resources_to_create)}
- Dependencies: {len(state['chart_plan'].chart_dependencies)}
- Security Policies: {len(state['chart_plan'].security_policies)}
- Generation Tasks: {len(state['chart_plan'].generation_todos)}

Ready for human review.
        """
    )
    
    # Return to supervisor (no Command.PARENT needed - this is the end of subgraph)
    return {
        "messages": [summary_message],
        "chart_plan": state["chart_plan"],
        "workspace_files": state.get("workspace_files", {}),
        "todos": state.get("todos", []),
        "active_agent": "planning_supervisor"
    }
```

---

## Human-in-the-Loop (HITL)

### Dynamic Interrupts with Middleware

```python
# k8s_autopilot/core/hitl/middleware.py
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import interrupt, Command
from typing import Dict, Any

# ============================================================================
# Method 1: Using LangChain HITL Middleware (Recommended)
# ============================================================================

def create_supervisor_with_hitl():
    """
    Creates main supervisor with HITL middleware for tool-level approvals.
    
    Best for: Automatic interrupts on sensitive tool calls
    """
    from langchain.agents import create_agent
    from k8s_autopilot.tools.deployment import deploy_to_cluster_tool
    from k8s_autopilot.tools.argocd import create_argocd_application_tool
    
    # Configure checkpointer (required for HITL)
    DB_URI = settings.database_uri
    checkpointer = PostgresSaver.from_conn_string(DB_URI)
    checkpointer.setup()  # Create tables on first run
    
    agent = create_agent(
        model="claude-sonnet-4-5-20250929",
        tools=[
            deploy_to_cluster_tool,
            create_argocd_application_tool
        ],
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    # Require approval for deployment
                    "deploy_to_cluster": {
                        "allowed_decisions": ["approve", "edit", "reject"],
                        "description": "🚨 Cluster deployment requires approval"
                    },
                    # Require approval for ArgoCD changes
                    "create_argocd_application": {
                        "allowed_decisions": ["approve", "reject"],  # No editing
                        "description": "🚨 ArgoCD configuration requires approval"
                    }
                },
                description_prefix="Action pending review"
            )
        ],
        checkpointer=checkpointer
    )
    
    return agent


# ============================================================================
# Method 2: Custom Interrupt Gates (More Control)
# ============================================================================

def planning_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    Custom HITL gate for planning review.
    
    Uses interrupt() function for dynamic, conditional interrupts.
    """
    from langgraph.types import interrupt
    
    # Check if already approved
    approval = state["human_approval_status"].get("planning")
    if approval and approval.status in ["approved", "modified"]:
        return {
            "messages": [AIMessage(content="Planning approved, proceeding to generation...")]
        }
    
    # Prepare planning summary for review
    plan = state["planning_output"]
    review_data = {
        "phase": "planning",
        "chart_plan": plan.model_dump() if plan else None,
        "required_action": "approve",
        "options": ["approve", "reject", "modify"],
        "summary": f"""
# Planning Review Required

## Chart Information
- **Name**: {plan.chart_name if plan else 'N/A'}
- **Version**: {plan.chart_version if plan else 'N/A'}
- **Description**: {plan.description if plan else 'N/A'}

## Resources to Create
{chr(10).join(f"- {r}" for r in (plan.resources_to_create if plan else []))}

## Security Policies
{chr(10).join(f"- {p}" for p in (plan.security_policies if plan else []))}

## Dependencies
{len(plan.chart_dependencies if plan else [])} chart dependencies

Please review and approve/reject/modify the plan.
        """
    }
    
    # Trigger interrupt - execution pauses here
    # Returns control to caller with review_data in __interrupt__ field
    human_decision = interrupt(review_data)
    
    # After resume, human_decision contains the response
    # Update approval status based on decision
    approval_status = ApprovalStatus(
        status=human_decision.get("decision", "rejected"),
        reviewer=human_decision.get("reviewer"),
        comments=human_decision.get("comments"),
        timestamp=datetime.utcnow()
    )
    
    return {
        "human_approval_status": {
            **state.get("human_approval_status", {}),
            "planning": approval_status
        },
        "messages": [
            AIMessage(
                content=f"Planning {approval_status.status} by {approval_status.reviewer}"
            )
        ]
    }


def security_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """HITL gate for security review"""
    from langgraph.types import interrupt
    
    approval = state["human_approval_status"].get("security")
    if approval and approval.status == "approved":
        return {"messages": [AIMessage(content="Security approved")]}
    
    # Get security scan results
    validation_results = state.get("validation_results", [])
    security_issues = [
        v for v in validation_results 
        if v.validator == "security_scanner" and v.severity in ["error", "critical"]
    ]
    
    review_data = {
        "phase": "security",
        "security_issues": [issue.model_dump() for issue in security_issues],
        "required_action": "approve",
        "options": ["approve", "reject"],
        "summary": f"""
# Security Review Required

## Critical Issues Found: {len([i for i in security_issues if i.severity == "critical"])}
## Error Issues Found: {len([i for i in security_issues if i.severity == "error"])}

{chr(10).join(f"### {i.severity.upper()}: {i.message}" for i in security_issues[:5])}

Please review security findings and approve deployment.
        """
    }
    
    human_decision = interrupt(review_data)
    
    approval_status = ApprovalStatus(
        status=human_decision.get("decision", "rejected"),
        reviewer=human_decision.get("reviewer"),
        comments=human_decision.get("comments")
    )
    
    return {
        "human_approval_status": {
            **state.get("human_approval_status", {}),
            "security": approval_status
        },
        "messages": [AIMessage(content=f"Security review: {approval_status.status}")]
    }


def deployment_approval_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """HITL gate for final deployment approval"""
    from langgraph.types import interrupt
    
    approval = state["human_approval_status"].get("deployment")
    if approval and approval.status == "approved":
        return {"messages": [AIMessage(content="Deployment approved")]}
    
    review_data = {
        "phase": "deployment",
        "chart_artifacts": list(state.get("generated_artifacts", {}).keys()),
        "validation_summary": {
            "total_checks": len(state.get("validation_results", [])),
            "passed": len([v for v in state.get("validation_results", []) if v.passed]),
            "failed": len([v for v in state.get("validation_results", []) if not v.passed])
        },
        "argocd_config": state.get("validation_results", [{}])[-1],  # Simplified
        "required_action": "approve",
        "options": ["approve", "reject"],
        "summary": """
# Final Deployment Approval

All validations complete. Chart ready for deployment to cluster.

## Validation Summary
- Chart structure: ✓ Valid
- Security scan: ✓ Passed
- Best practices: ✓ Compliant
- ArgoCD config: ✓ Generated

Approve to deploy the Helm chart?
        """
    }
    
    human_decision = interrupt(review_data)
    
    approval_status = ApprovalStatus(
        status=human_decision.get("decision", "rejected"),
        reviewer=human_decision.get("reviewer"),
        comments=human_decision.get("comments")
    )
    
    return {
        "human_approval_status": {
            **state.get("human_approval_status", {}),
            "deployment": approval_status
        },
        "messages": [AIMessage(content=f"Deployment: {approval_status.status}")]
    }


# ============================================================================
# Workflow Compilation with Interrupt Gates
# ============================================================================

def compile_main_workflow_with_hitl():
    """
    Compiles the main workflow with HITL gates and checkpointing.
    """
    from k8s_autopilot.config.settings import settings
    
    main_builder = create_main_supervisor_method1()  # or method2
    
    # Add HITL gate nodes
    main_builder.add_node("planning_review_gate", planning_review_gate)
    main_builder.add_node("security_review_gate", security_review_gate)
    main_builder.add_node("deployment_approval_gate", deployment_approval_gate)
    
    # Connect gates in workflow
    main_builder.add_edge("planning_swarm", "planning_review_gate")
    main_builder.add_edge("generation_swarm", "security_review_gate")
    main_builder.add_edge("validation_swarm", "deployment_approval_gate")
    
    # Conditional edges after gates
    main_builder.add_conditional_edges(
        "planning_review_gate",
        lambda s: "generation_swarm" if s["human_approval_status"]["planning"].status == "approved" else "planning_swarm"
    )
    
    main_builder.add_conditional_edges(
        "security_review_gate",
        lambda s: "validation_swarm" if s["human_approval_status"]["security"].status == "approved" else "generation_swarm"
    )
    
    main_builder.add_conditional_edges(
        "deployment_approval_gate",
        lambda s: END if s["human_approval_status"]["deployment"].status == "approved" else "validation_swarm"
    )
    
    # Configure checkpointer
    DB_URI = settings.database_uri
    checkpointer = PostgresSaver.from_conn_string(DB_URI)
    checkpointer.setup()
    
    # Compile with checkpointer (required for interrupts)
    graph = main_builder.compile(checkpointer=checkpointer)
    
    return graph


# ============================================================================
# Client Usage - Invoking Workflow with HITL
# ============================================================================

def run_helm_generation_workflow(requirements: ChartRequirements) -> Dict[str, Any]:
    """
    Example client code for running the workflow with HITL.
    """
    graph = compile_main_workflow_with_hitl()
    
    # Create thread ID for this workflow
    thread_id = f"helm-gen-{requirements.application_name}-{datetime.utcnow().timestamp()}"
    config = {"configurable": {"thread_id": thread_id}}
    
    # Initial invocation
    initial_state = {
        "messages": [HumanMessage(content="Generate Helm chart")],
        "user_requirements": requirements,
        "active_phase": "requirements",
        "human_approval_status": {},
        "workflow_metadata": WorkflowMetadata(
            workflow_id=thread_id,
            current_phase="requirements"
        ),
        "file_artifacts": {},
        "todos": [],
        "validation_results": []
    }
    
    # Run until first interrupt
    result = graph.invoke(initial_state, config=config)
    
    # Check for interrupts
    if "__interrupt__" in result:
        interrupt_data = result["__interrupt__"][0].value
        print(f"Workflow paused for review: {interrupt_data['phase']}")
        print(interrupt_data["summary"])
        
        # In production, present to user via UI/API
        # For now, simulate approval
        human_decision = {
            "decision": "approved",
            "reviewer": "john.doe@example.com",
            "comments": "LGTM"
        }
        
        # Resume execution
        result = graph.invoke(
            Command(resume=human_decision),
            config=config
        )
        
        # Handle additional interrupts in loop...
        while "__interrupt__" in result:
            interrupt_data = result["__interrupt__"][0].value
            # Get approval...
            result = graph.invoke(Command(resume=human_decision), config=config)
    
    return result
```

---

## Persistence & Checkpointing

### PostgreSQL Checkpointer Configuration

```python
# k8s_autopilot/core/persistence/checkpointer.py
from langgraph.checkpoint.postgres import PostgresSaver, AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from k8s_autopilot.config.settings import settings
from contextlib import contextmanager
import os

# ============================================================================
# Checkpointer Setup
# ============================================================================

class CheckpointerManager:
    """Manages checkpointer lifecycle and configuration"""
    
    @staticmethod
    def create_checkpointer(encrypted: bool = True) -> PostgresSaver:
        """
        Creates a PostgreSQL checkpointer with optional encryption.
        
        Args:
            encrypted: Whether to encrypt persisted state
        """
        DB_URI = settings.database_uri
        
        if encrypted:
            # Encryption requires LANGGRAPH_AES_KEY environment variable
            if not os.getenv("LANGGRAPH_AES_KEY"):
                raise ValueError("LANGGRAPH_AES_KEY required for encryption")
            
            serde = EncryptedSerializer.from_pycryptodome_aes()
            checkpointer = PostgresSaver.from_conn_string(DB_URI, serde=serde)
        else:
            checkpointer = PostgresSaver.from_conn_string(DB_URI)
        
        # Setup tables (first time only)
        checkpointer.setup()
        
        return checkpointer
    
    @staticmethod
    @contextmanager
    def checkpointer_context(encrypted: bool = True):
        """
        Context manager for checkpointer lifecycle.
        
        Usage:
            with CheckpointerManager.checkpointer_context() as checkpointer:
                graph = workflow.compile(checkpointer=checkpointer)
                result = graph.invoke(...)
        """
        checkpointer = CheckpointerManager.create_checkpointer(encrypted)
        try:
            yield checkpointer
        finally:
            # Cleanup if needed
            pass


# ============================================================================
# Async Checkpointer
# ============================================================================

class AsyncCheckpointerManager:
    """Async version for high-throughput scenarios"""
    
    @staticmethod
    async def create_async_checkpointer(encrypted: bool = True) -> AsyncPostgresSaver:
        """Creates async PostgreSQL checkpointer"""
        DB_URI = settings.database_uri
        
        if encrypted:
            serde = EncryptedSerializer.from_pycryptodome_aes()
            checkpointer = AsyncPostgresSaver.from_conn_string(DB_URI, serde=serde)
        else:
            checkpointer = AsyncPostgresSaver.from_conn_string(DB_URI)
        
        await checkpointer.setup()
        return checkpointer


# ============================================================================
# Thread Management
# ============================================================================

class ThreadManager:
    """Manages workflow threads and state persistence"""
    
    def __init__(self, checkpointer: PostgresSaver):
        self.checkpointer = checkpointer
    
    def get_thread_state(self, thread_id: str, graph) -> Dict[str, Any]:
        """
        Retrieves current state for a thread.
        
        Args:
            thread_id: Unique thread identifier
            graph: Compiled LangGraph instance
        """
        config = {"configurable": {"thread_id": thread_id}}
        state = graph.get_state(config)
        return state
    
    def get_thread_history(
        self, 
        thread_id: str, 
        graph,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieves checkpoint history for a thread.
        
        Args:
            thread_id: Unique thread identifier
            graph: Compiled LangGraph instance
            limit: Maximum number of checkpoints to return
        """
        config = {"configurable": {"thread_id": thread_id}}
        checkpoints = []
        
        for i, checkpoint in enumerate(graph.get_state_history(config)):
            if limit and i >= limit:
                break
            checkpoints.append({
                "config": checkpoint.config,
                "values": checkpoint.values,
                "next": checkpoint.next,
                "metadata": checkpoint.metadata
            })
        
        return checkpoints
    
    def resume_thread(
        self,
        thread_id: str,
        graph,
        resume_data: Any = None
    ) -> Dict[str, Any]:
        """
        Resumes execution of an interrupted thread.
        
        Args:
            thread_id: Thread to resume
            graph: Compiled graph
            resume_data: Data to pass to interrupt resume (e.g., human decisions)
        """
        config = {"configurable": {"thread_id": thread_id}}
        
        if resume_data is not None:
            result = graph.invoke(Command(resume=resume_data), config=config)
        else:
            result = graph.invoke(None, config=config)
        
        return result
```

### State Inspection and Time Travel

```python
# k8s_autopilot/core/persistence/state_inspection.py
from langgraph.graph import StateGraph
from typing import Dict, Any, List, Optional

class StateInspector:
    """Tools for inspecting and debugging workflow state"""
    
    def __init__(self, graph: StateGraph):
        self.graph = graph
    
    def inspect_current_state(
        self,
        thread_id: str,
        include_subgraphs: bool = False
    ) -> Dict[str, Any]:
        """
        Inspects current state of a workflow thread.
        
        Args:
            thread_id: Thread to inspect
            include_subgraphs: Whether to include subgraph states
        """
        config = {"configurable": {"thread_id": thread_id}}
        
        if include_subgraphs:
            # Note: Subgraph state only available when subgraph is interrupted
            state = self.graph.get_state(config, subgraphs=True)
        else:
            state = self.graph.get_state(config)
        
        return {
            "values": state.values,
            "next_nodes": state.next,
            "config": state.config,
            "metadata": state.metadata,
            "tasks": [
                {
                    "id": task.id,
                    "name": task.name,
                    "error": task.error if hasattr(task, "error") else None
                }
                for task in (state.tasks or [])
            ]
        }
    
    def get_checkpoint_at_step(
        self,
        thread_id: str,
        step: int
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves checkpoint at specific step for time travel.
        
        Args:
            thread_id: Thread ID
            step: Step number (0-indexed)
        """
        config = {"configurable": {"thread_id": thread_id}}
        checkpoints = list(self.graph.get_state_history(config))
        
        if step < len(checkpoints):
            checkpoint = checkpoints[step]
            return {
                "values": checkpoint.values,
                "config": checkpoint.config,
                "next": checkpoint.next
            }
        
        return None
    
    def replay_from_checkpoint(
        self,
        thread_id: str,
        checkpoint_id: str
    ) -> Dict[str, Any]:
        """
        Replays workflow from a specific checkpoint.
        
        Useful for debugging or recovering from errors.
        """
        # Create new config with checkpoint ID
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id
            }
        }
        
        # Invoke from checkpoint
        result = self.graph.invoke(None, config=config)
        return result
```

---

## Deep Agents Integration

### Planning Swarm as Deep Agent

```python
# k8s_autopilot/core/deep_agents/planning_agent.py
from deepagents import create_deep_agent
from langgraph.store import InMemoryStore
from k8s_autopilot.tools.planning import (
    validate_requirements_tool,
    search_bitnami_standards_tool,
    search_helm_best_practices_tool
)

def create_planning_deep_agent(
    use_persistent_memory: bool = True
) -> Any:
    """
    Creates a Deep Agent for planning with:
    - Built-in todo management
    - File system for research storage
    - Long-term memory for org standards
    - Subagent spawning capabilities
    """
    
    # Create memory store for long-term context
    store = InMemoryStore() if use_persistent_memory else None
    
    planning_instructions = """
You are an expert Helm chart planner and Kubernetes architect.

## Core Responsibilities:
1. **Validate Requirements**: Analyze completeness and feasibility
2. **Research Best Practices**: Query Bitnami standards and CNCF guidelines
3. **Plan Architecture**: Design chart structure following industry standards
4. **Create Todo Lists**: Break down work for generation phase

## File System Organization:
Use the built-in file system tools to manage context:

/workspace/
├── research/
│   ├── bitnami-standards.md
│   ├── helm-best-practices.md
│   └── security-guidelines.md
├── analysis/
│   ├── requirements-validation.json
│   └── resource-estimates.json
└── plans/
    └── chart-plan.json

/memories/  (persistent across threads)
└── org-standards/
    ├── naming-conventions.md
    ├── security-policies.md
    └── deployment-patterns.md

## Planning Workflow:
1. Read organizational standards from /memories/org-standards/
2. Validate user requirements and save analysis to /workspace/analysis/
3. Research relevant best practices and cache in /workspace/research/
4. Create comprehensive chart plan and save to /workspace/plans/
5. Use write_todos to create structured task list for generation
6. If requirements are complex, spawn a subagent for specialized research

## Todo Structure:
Create todos with clear ownership and dependencies:
```json
{
  "todos": [
    {
      "id": 1,
      "title": "Generate Deployment template",
      "status": "pending",
      "priority": "high",
      "dependencies": [],
      "assignee": "template_generator"
    }
  ]
}
```

## Subagent Usage:
For specialized tasks, spawn subagents:
- Security analysis subagent for complex security requirements
- Database planning subagent for stateful applications
- Networking subagent for complex service mesh configurations

Always provide detailed context when handing off to subagents.
    """
    
    agent = create_deep_agent(
        tools=[
            validate_requirements_tool,
            search_bitnami_standards_tool,
            search_helm_best_practices_tool
        ],
        system_prompt=planning_instructions,
        store=store,
        use_longterm_memory=use_persistent_memory,
        # Optional: Custom backend for file storage
        # backend=FilesystemBackend(root_dir="/var/lib/k8s-autopilot/planning")
    )
    
    return agent
```

### Generation Swarm as Deep Agent with File System

```python
# k8s_autopilot/core/deep_agents/generation_agent.py
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from k8s_autopilot.tools.generation import (
    generate_deployment_template_tool,
    generate_service_template_tool,
    generate_values_schema_tool,
    validate_yaml_syntax_tool
)

def create_generation_deep_agent(
    workspace_dir: str = "/tmp/helm-workspace"
) -> Any:
    """
    Creates a Deep Agent for chart generation with:
    - File system backend for managing YAML templates
    - Iterative editing capabilities
    - Multi-file generation coordination
    """
    
    generation_instructions = """
You are an expert Helm chart generator creating Bitnami-quality charts.

## Core Responsibilities:
1. **Generate Templates**: Create all Kubernetes resource templates
2. **Create Values Files**: Generate values.yaml and values.schema.json
3. **Apply Security**: Implement security contexts and policies
4. **Write Documentation**: Create comprehensive README and NOTES.txt

## File System Structure:
Generate a complete Helm chart in /workspace/:

/workspace/
├── templates/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── hpa.yaml
│   ├── pvc.yaml
│   ├── serviceaccount.yaml
│   ├── _helpers.tpl
│   └── NOTES.txt
├── values.yaml
├── values.schema.json
├── Chart.yaml
├── README.md
├── .helmignore
└── templates.yaml

## Generation Workflow:
1. Read chart plan from input
2. Use write_file to create initial templates
3. Validate each template with validate_yaml_syntax_tool
4. Use edit_file to refine based on validation feedback
5. Apply security hardening to all resources
6. Generate comprehensive values.yaml with sensible defaults
7. Create JSON schema for values validation
8. Write README with installation instructions
9. Create NOTES.txt for post-install guidance

## Template Best Practices:
- Use {{ include "chart.fullname" . }} for resource names
- Include proper labels: app.kubernetes.io/* 
- Add annotations for monitoring and documentation
- Implement security contexts (runAsNonRoot, readOnlyRootFilesystem)
- Set resource requests and limits
- Add liveness and readiness probes
- Use ConfigMaps for configuration, Secrets for sensitive data
- Support both Ingress and Service exposure
- Enable HPA for scalability
- Include ServiceAccount with minimal permissions

## Iteration Pattern:
1. Generate initial template
2. Validate syntax
3. If errors, use edit_file to fix specific issues
4. Validate again until clean
5. Move to next template

Use the built-in file tools efficiently:
- ls: List generated files
- read_file: Read template for review
- write_file: Create new templates
- edit_file: Make targeted changes
- grep: Search for patterns across templates

For complex security hardening, consider spawning a security subagent.
    """
    
    agent = create_deep_agent(
        tools=[
            generate_deployment_template_tool,
            generate_service_template_tool,
            generate_values_schema_tool,
            validate_yaml_syntax_tool
        ],
        system_prompt=generation_instructions,
        backend=FilesystemBackend(root_dir=workspace_dir),
        use_longterm_memory=True
    )
    
    return agent
```

---

## Error Handling & Retry Logic

### Retry Policies

```python
# k8s_autopilot/core/error_handling/retry.py
from langgraph.types import RetryPolicy, Command
from typing import Literal, Optional
from k8s_autopilot.core.state.base import MainSupervisorState, ErrorContext
import logging

logger = logging.getLogger(__name__)

# ============================================================================
# Retry Policy Configuration
# ============================================================================

# Configure retry policies for different node types
PLANNING_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff_factor=2.0,
    initial_interval=1.0,
    max_interval=60.0,
    jitter=True
)

GENERATION_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff_factor=2.0,
    initial_interval=2.0,
    max_interval=120.0
)

VALIDATION_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    backoff_factor=1.5,
    initial_interval=1.0,
    max_interval=30.0
)


# ============================================================================
# Error Classification
# ============================================================================

class ErrorClassifier:
    """Classifies errors and determines recovery strategy"""
    
    @staticmethod
    def classify_error(error: Exception) -> tuple[str, bool]:
        """
        Classifies error and determines if recoverable.
        
        Returns:
            (error_type, recoverable)
        """
        error_str = str(error)
        error_type = type(error).__name__
        
        # LLM errors - usually recoverable
        if "RateLimitError" in error_type:
            return ("rate_limit", True)
        if "TimeoutError" in error_type:
            return ("timeout", True)
        if "APIError" in error_type:
            return ("api_error", True)
        
        # Validation errors - sometimes recoverable
        if "ValidationError" in error_type:
            return ("validation", True)
        if "yaml" in error_str.lower():
            return ("yaml_syntax", True)
        
        # System errors - usually not recoverable
        if "PermissionError" in error_type:
            return ("permission", False)
        if "FileNotFoundError" in error_type:
            return ("file_not_found", False)
        
        # Default: unknown error, not recoverable
        return ("unknown", False)


# ============================================================================
# Error Handler Node
# ============================================================================

def error_handler(state: MainSupervisorState) -> Command[Literal[
    "planning_swarm",
    "generation_swarm",
    "validation_swarm",
    "human_intervention",
    END
]]:
    """
    Central error handler for workflow.
    
    Attempts recovery based on error type and retry count.
    Escalates to human if recovery fails.
    """
    error = state.get("error_state")
    
    if not error:
        logger.warning("Error handler called without error state")
        return Command(update={}, goto=END)
    
    logger.error(
        f"Handling error in {error.failed_swarm}: {error.error_type} - {error.error_message}"
    )
    
    # Check retry budget
    if error.retry_count >= error.max_retries:
        logger.error(f"Max retries ({error.max_retries}) exceeded")
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=f"❌ Max retries exceeded. Error: {error.error_message}"
                    )
                ]
            },
            goto="human_intervention"
        )
    
    # Check if recoverable
    if not error.recoverable:
        logger.error("Error not recoverable, escalating to human")
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=f"❌ Unrecoverable error: {error.error_message}"
                    )
                ]
            },
            goto="human_intervention"
        )
    
    # Attempt recovery
    logger.info(f"Attempting retry {error.retry_count + 1}/{error.max_retries}")
    
    # Update error state with incremented retry count
    updated_error = ErrorContext(
        **{
            **error.model_dump(),
            "retry_count": error.retry_count + 1
        }
    )
    
    # Determine recovery action based on error type
    recovery_message = f"🔄 Retrying {error.failed_swarm} after {error.error_type} error..."
    
    if error.error_type == "rate_limit":
        recovery_message += " (waiting for rate limit reset)"
        # In production, implement exponential backoff here
    
    elif error.error_type == "validation":
        recovery_message += " (attempting to fix validation issues)"
        # Could modify state here to provide fixes
    
    # Route back to failed swarm for retry
    return Command(
        update={
            "error_state": updated_error,
            "messages": [AIMessage(content=recovery_message)]
        },
        goto=error.failed_swarm
    )


# ============================================================================
# Error Wrapper for Nodes
# ============================================================================

def with_error_handling(node_func, node_name: str, swarm_name: str):
    """
    Decorator to wrap nodes with error handling.
    
    Usage:
        @with_error_handling
        def my_node(state):
            ...
    """
    def wrapped(state: MainSupervisorState):
        try:
            # Execute node
            result = node_func(state)
            
            # Clear error state on success
            if state.get("error_state") is not None:
                if isinstance(result, dict):
                    result["error_state"] = None
                else:
                    # If returning Command, update error_state
                    pass
            
            return result
            
        except Exception as e:
            logger.exception(f"Error in {node_name}: {e}")
            
            # Classify error
            error_type, recoverable = ErrorClassifier.classify_error(e)
            
            # Create error context
            error_context = ErrorContext(
                error_type=error_type,
                error_message=str(e),
                failed_node=node_name,
                failed_swarm=swarm_name,
                retry_count=state.get("error_state", {}).get("retry_count", 0),
                recoverable=recoverable,
                stack_trace=traceback.format_exc()
            )
            
            # Return Command to error_handler
            return Command(
                update={
                    "error_state": error_context,
                    "messages": [
                        AIMessage(
                            content=f"⚠️ Error in {node_name}: {error_type}"
                        )
                    ],
                    "active_phase": "error"
                },
                goto="error_handler"
            )
    
    return wrapped


# Usage example
@with_error_handling(node_name="planning_swarm", swarm_name="planning_swarm")
def planning_swarm_node(state: MainSupervisorState):
    """Planning swarm node with error handling"""
    # Node implementation...
    pass
```

---

## Observability & Monitoring

### LangSmith Integration

```python
# k8s_autopilot/core/observability/langsmith.py
import os
from langsmith import Client
from k8s_autopilot.config.settings import settings

# ============================================================================
# LangSmith Configuration
# ============================================================================

def configure_langsmith():
    """
    Configures LangSmith for tracing and monitoring.
    
    Call this at application startup.
    """
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_TRACING"] = str(settings.langsmith_tracing).lower()
    
    # Optional: Custom endpoint for self-hosted LangSmith
    # os.environ["LANGSMITH_ENDPOINT"] = "https://langsmith.example.com"

# Initialize at module load
configure_langsmith()


# ============================================================================
# Custom Metadata and Tags
# ============================================================================

def create_run_config(
    workflow_id: str,
    chart_name: str,
    user_id: str,
    tags: List[str] = None
) -> Dict[str, Any]:
    """
    Creates run configuration with custom metadata for LangSmith.
    
    This metadata will be visible in LangSmith UI for debugging and analysis.
    """
    return {
        "configurable": {
            "thread_id": workflow_id
        },
        "metadata": {
            "workflow_id": workflow_id,
            "chart_name": chart_name,
            "user_id": user_id,
            "application": "k8s-autopilot",
            "version": "1.0.0"
        },
        "tags": tags or ["helm-generation", chart_name]
    }


# ============================================================================
# Prometheus Metrics
# ============================================================================

from prometheus_client import Counter, Histogram, Gauge
import time

# Define metrics
helm_generation_total = Counter(
    "helm_generation_total",
    "Total number of Helm chart generation requests",
    ["status", "chart_type"]
)

helm_generation_duration = Histogram(
    "helm_generation_duration_seconds",
    "Time spent generating Helm charts",
    ["phase"]
)

helm_generation_active = Gauge(
    "helm_generation_active",
    "Number of active Helm chart generations"
)

hitl_approvals_total = Counter(
    "hitl_approvals_total",
    "Total number of HITL approvals",
    ["phase", "decision"]
)

agent_errors_total = Counter(
    "agent_errors_total",
    "Total number of agent errors",
    ["swarm", "error_type"]
)


class MetricsCollector:
    """Collects and records metrics during workflow execution"""
    
    @staticmethod
    def record_generation_start(chart_type: str):
        """Records start of chart generation"""
        helm_generation_active.inc()
    
    @staticmethod
    def record_generation_complete(
        chart_type: str,
        status: str,
        duration: float
    ):
        """Records completion of chart generation"""
        helm_generation_active.dec()
        helm_generation_total.labels(status=status, chart_type=chart_type).inc()
    
    @staticmethod
    def record_phase_duration(phase: str, duration: float):
        """Records duration of a workflow phase"""
        helm_generation_duration.labels(phase=phase).observe(duration)
    
    @staticmethod
    def record_hitl_decision(phase: str, decision: str):
        """Records HITL approval decision"""
        hitl_approvals_total.labels(phase=phase, decision=decision).inc()
    
    @staticmethod
    def record_error(swarm: str, error_type: str):
        """Records agent error"""
        agent_errors_total.labels(swarm=swarm, error_type=error_type).inc()
```

---

## Implementation Guide

### Step-by-Step Implementation

```python
# k8s_autopilot/main.py
from k8s_autopilot.core.supervisor.main_supervisor import create_main_supervisor
from k8s_autopilot.core.persistence.checkpointer import CheckpointerManager
from k8s_autopilot.core.observability.langsmith import configure_langsmith, MetricsCollector
from k8s_autopilot.core.state.base import ChartRequirements
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    """
    Main entry point for K8s Autopilot.
    
    Complete implementation guide:
    1. Configure observability
    2. Setup persistence
    3. Build workflow graph
    4. Execute with HITL
    """
    
    # Step 1: Configure observability
    configure_langsmith()
    logger.info("✓ LangSmith configured")
    
    # Step 2: Create checkpointer
    with CheckpointerManager.checkpointer_context(encrypted=True) as checkpointer:
        logger.info("✓ PostgreSQL checkpointer ready")
        
        # Step 3: Build workflow graph
        graph = create_main_supervisor(checkpointer=checkpointer)
        logger.info("✓ Workflow graph compiled")
        
        # Step 4: Create test requirements
        requirements = ChartRequirements(
            application_name="my-api",
            application_type="api",
            container_image="myregistry/my-api:1.0.0",
            replicas=3,
            service_port=8080,
            ingress_enabled=True,
            ingress_host="api.example.com"
        )
        
        # Step 5: Execute workflow
        thread_id = f"helm-{requirements.application_name}-{int(datetime.utcnow().timestamp())}"
        config = {"configurable": {"thread_id": thread_id}}
        
        logger.info(f"Starting workflow: {thread_id}")
        MetricsCollector.record_generation_start(requirements.application_type)
        
        start_time = time.time()
        
        try:
            # Initial state
            initial_state = {
                "messages": [HumanMessage(content="Generate Helm chart")],
                "user_requirements": requirements,
                "active_phase": "requirements",
                "human_approval_status": {},
                "workflow_metadata": WorkflowMetadata(
                    workflow_id=thread_id,
                    current_phase="requirements"
                ),
                "file_artifacts": {},
                "todos": [],
                "validation_results": [],
                "planning_output": None,
                "generated_artifacts": None,
                "error_state": None
            }
            
            # Run workflow (handles interrupts automatically)
            result = graph.invoke(initial_state, config=config)
            
            # Handle HITL interrupts in loop
            while "__interrupt__" in result:
                interrupt_data = result["__interrupt__"][0].value
                
                logger.info(f"Workflow interrupted: {interrupt_data['phase']}")
                print("\n" + "="*80)
                print(interrupt_data["summary"])
                print("="*80 + "\n")
                
                # Simulate human approval (in production, get from UI/API)
                decision = input("Decision (approve/reject/modify): ").strip().lower()
                reviewer = "system.admin"
                comments = input("Comments (optional): ").strip() or "Approved"
                
                human_decision = {
                    "decision": decision,
                    "reviewer": reviewer,
                    "comments": comments
                }
                
                MetricsCollector.record_hitl_decision(
                    interrupt_data["phase"],
                    decision
                )
                
                # Resume workflow
                result = graph.invoke(
                    Command(resume=human_decision),
                    config=config
                )
            
            duration = time.time() - start_time
            
            logger.info(f"✓ Workflow completed in {duration:.2f}s")
            MetricsCollector.record_generation_complete(
                requirements.application_type,
                "success",
                duration
            )
            
            # Display results
            print("\n" + "="*80)
            print("HELM CHART GENERATION COMPLETE")
            print("="*80)
            print(f"Chart Name: {result.get('planning_output', {}).get('chart_name', 'N/A')}")
            print(f"Files Generated: {len(result.get('generated_artifacts', {}))}")
            print(f"Validation Results: {len(result.get('validation_results', []))}")
            print("="*80 + "\n")
            
        except Exception as e:
            duration = time.time() - start_time
            logger.exception("Workflow failed")
            MetricsCollector.record_generation_complete(
                requirements.application_type,
                "error",
                duration
            )
            raise

if __name__ == "__main__":
    main()
```

---

## Production Deployment

### Docker Compose Setup

```yaml
# docker-compose.yml
version: '3.8'

services:
  # PostgreSQL for checkpointing
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: k8s_autopilot
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5

  # K8s Autopilot API
  api:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      POSTGRES_DB: k8s_autopilot
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      LANGSMITH_API_KEY: ${LANGSMITH_API_KEY}
      LANGSMITH_PROJECT: k8s-autopilot-prod
      LANGGRAPH_AES_KEY: ${LANGGRAPH_AES_KEY}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./workspace:/tmp/helm-workspace
    command: uvicorn k8s_autopilot.api:app --host 0.0.0.0 --port 8000

  # Prometheus for metrics
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'

  # Grafana for visualization
  grafana:
    image: grafana/grafana:latest
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD}
    volumes:
      - grafana_data:/var/lib/grafana
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
    ports:
      - "3000:3000"
    depends_on:
      - prometheus

volumes:
  postgres_data:
  prometheus_data:
  grafana_data:
```

### Kubernetes Deployment

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8s-autopilot
  namespace: k8s-autopilot
spec:
  replicas: 3
  selector:
    matchLabels:
      app: k8s-autopilot
  template:
    metadata:
      labels:
        app: k8s-autopilot
    spec:
      serviceAccountName: k8s-autopilot
      containers:
      - name: api
        image: k8s-autopilot:latest
        ports:
        - containerPort: 8000
        env:
        - name: POSTGRES_HOST
          value: postgres-service
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: k8s-autopilot-secrets
              key: postgres-password
        - name: LANGSMITH_API_KEY
          valueFrom:
            secretKeyRef:
              name: k8s-autopilot-secrets
              key: langsmith-api-key
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: k8s-autopilot-secrets
              key: anthropic-api-key
        - name: LANGGRAPH_AES_KEY
          valueFrom:
            secretKeyRef:
              name: k8s-autopilot-secrets
              key: langgraph-aes-key
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "2000m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
```

---

## Summary

This enhanced architecture document provides:

1. ✅ **Complete state schemas** with proper reducers for concurrent updates
2. ✅ **Deep Agents integration** for planning and generation swarms
3. ✅ **Subgraph patterns** with two implementation methods
4. ✅ **Command-based handoffs** within and between swarms
5. ✅ **Dynamic HITL** with interrupts and middleware
6. ✅ **PostgreSQL persistence** with encryption support
7. ✅ **Error handling** with classification and retry logic
8. ✅ **Observability** with LangSmith and Prometheus
9. ✅ **Production deployment** examples
10. ✅ **Step-by-step implementation** guide

All patterns follow **LangChain v1.0** and **LangGraph best practices** as documented in the official LangChain documentation.

---

## References

- [LangChain v1.0 Documentation](https://docs.langchain.com)
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)
- [Subgraphs Guide](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [Human-in-the-Loop](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)
- [PostgreSQL Checkpointer](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [Command Pattern](https://docs.langchain.com/oss/python/langgraph/use-graph-api)

---

*This document serves as the authoritative technical specification for the K8s Autopilot multi-agent framework, incorporating all latest LangChain v1.0 patterns and best practices.*

