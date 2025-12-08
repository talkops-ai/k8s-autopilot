# Planner Agent Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Main Supervisor Agent](#main-supervisor-agent)
4. [Sub-Agents](#sub-agents)
5. [Tools](#tools)
6. [State Management](#state-management)
7. [Workflow](#workflow)
8. [Prompts](#prompts)
9. [Integration](#integration)
10. [Examples](#examples)

---

## Overview

The **Planner Agent** (`k8sAutopilotPlanningDeepAgent`) is a specialized deep agent responsible for analyzing user requirements and designing Kubernetes architecture for Helm chart generation. It operates as part of the k8s-autopilot system's planning phase, transforming natural language requirements into structured deployment plans.

### Key Responsibilities

- **Requirement Extraction**: Parse user queries to extract deployment parameters (application type, framework, resources, exposure, etc.)
- **Gap Detection**: Identify missing critical information and request clarifications through Human-in-the-Loop (HITL) interactions
- **Requirements Validation**: Validate completeness and correctness of extracted requirements
- **Architecture Planning**: Design production-ready Kubernetes architectures following Bitnami standards
- **Resource Estimation**: Calculate CPU/memory requirements across dev, staging, and production environments
- **Scaling Strategy**: Define Horizontal Pod Autoscaler (HPA) configurations
- **Dependency Analysis**: Identify required Helm chart dependencies, init containers, sidecars, and lifecycle hooks

### Technology Stack

- **Framework**: LangChain Deep Agents (`create_deep_agent`)
- **State Management**: LangGraph with `PlanningSwarmState`
- **LLM Integration**: Configurable LLM providers via `LLMProvider`
- **Checkpointing**: Memory-based checkpointing for state persistence

---

## Architecture

The Planner Agent follows a **Deep Agent** architecture pattern with a supervisor agent coordinating specialized sub-agents:

```
┌─────────────────────────────────────────────────────────────┐
│              Planning Supervisor Agent                       │
│  (k8sAutopilotPlanningDeepAgent)                            │
│  - Extracts requirements from user query                     │
│  - Detects gaps and requests clarifications                  │
│  - Delegates to sub-agents                                   │
│  - Compiles final chart plan                                 │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
        ▼                     ▼
┌──────────────────┐  ┌──────────────────────┐
│ Requirements     │  │ Architecture         │
│ Analyzer        │  │ Planner              │
│ Sub-Agent       │  │ Sub-Agent            │
│                 │  │                      │
│ Tools:          │  │ Tools:               │
│ - parse_        │  │ - analyze_           │
│   requirements  │  │   application_       │
│ - classify_     │  │   requirements       │
│   complexity    │  │ - design_kubernetes_│
│ - validate_     │  │   architecture       │
│   requirements  │  │ - estimate_resources │
│                 │  │ - define_scaling_    │
│                 │  │   strategy           │
│                 │  │ - check_dependencies │
└──────────────────┘  └──────────────────────┘
```

### Component Overview

1. **Main Supervisor** (`k8sAutopilotPlanningDeepAgent`)
   - Orchestrates the planning workflow
   - Handles HITL interactions via `request_human_input` tool
   - Delegates specialized tasks to sub-agents

2. **Requirements Analyzer Sub-Agent**
   - Parses natural language into structured requirements
   - Classifies deployment complexity
   - Validates requirement completeness

3. **Architecture Planner Sub-Agent**
   - Analyzes application characteristics
   - Designs Kubernetes resource architecture
   - Estimates resource requirements
   - Defines scaling strategies
   - Identifies dependencies

---

## Main Supervisor Agent

### Class: `k8sAutopilotPlanningDeepAgent`

**Location**: `k8s_autopilot/core/agents/helm_generator/planner/planning_swarm.py`

#### Initialization

```python
def __init__(
    self,
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "planner_sub_supervisor_agent",
    memory: Optional[MemorySaver] = None
)
```

**Parameters**:
- `config`: Configuration object (uses centralized `Config` system)
- `custom_config`: Custom configuration overrides
- `name`: Agent identifier for routing
- `memory`: Checkpointer instance for state persistence

**Initialization Steps**:
1. Loads LLM configuration from centralized config
2. Creates main LLM model for sub-agents
3. Creates deep agent LLM model for supervisor
4. Initializes sub-agents (`requirements_analyzer`, `architecture_planner`)
5. Defines planning supervisor prompt

#### Key Methods

##### `build_graph() -> StateGraph`

Builds the deep agent graph using `create_deep_agent`:

```python
self.planning_agent = create_deep_agent(
    model=self.deep_agent_model,
    system_prompt=self._planner_prompt,
    tools=[self.request_human_input],  # HITL tool
    subagents=self._sub_agents,
    checkpointer=self.memory,
    context_schema=PlanningSwarmState,
    middleware=[PlanningStateMiddleware()],  # Exposes state to tools
)
```

**Features**:
- Uses `context_schema` to define state structure
- Middleware ensures state fields are available to tools
- Supports checkpointing for state persistence

##### `request_human_input()` Tool

**Purpose**: Pause execution and request human clarification when gaps are detected.

**Signature**:
```python
@tool
def request_human_input(
    question: str,
    context: Optional[str] = None,
    phase: Optional[str] = None,
    runtime: ToolRuntime[None, PlanningSwarmState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = ""
) -> Command
```

**Behavior**:
- Creates interrupt payload with question and context
- Calls `interrupt()` to pause execution
- Waits for human response
- Updates state with `updated_user_requirements` and `question_asked`
- Returns `Command` with state updates

**Critical Rule**: This is the **ONLY** way to request human input. Writing questions as text output will NOT pause the workflow.

#### Middleware

##### `PlanningStateMiddleware`

**Purpose**: Exposes `PlanningSwarmState` fields to tools when using `create_deep_agent`.

**Implementation**:
```python
class PlanningStateMiddleware(AgentMiddleware):
    state_schema = PlanningSwarmState
    tools = [
        parse_requirements,
        classify_complexity,
        validate_requirements,
        analyze_application_requirements,
        design_kubernetes_architecture,
        estimate_resources,
        define_scaling_strategy,
        check_dependencies
    ]
```

**Why Needed**: `create_deep_agent` uses `context_schema`, but tools need access to state via `runtime.state`. Middleware bridges this gap.

##### `ValidateRequirementsHITLMiddleware`

**Purpose**: Intercepts `validate_requirements` tool output and triggers HITL if clarifications are needed.

**Behavior**:
- Executes `validate_requirements` tool
- Checks if `clarifications_needed` list is non-empty
- Triggers interrupt with clarification questions
- Updates state with user feedback
- Merges feedback into `updated_user_requirements`

---

## Sub-Agents

### 1. Requirements Analyzer Sub-Agent

**Purpose**: Parse, classify, and validate Helm chart requirements.

**Tools**:
1. `parse_requirements` - Extract structured requirements from natural language
2. `classify_complexity` - Assess deployment complexity (simple/medium/complex)
3. `validate_requirements` - Check completeness and flag missing fields

**Prompt**: `REQUIREMENT_ANALYZER_SUBAGENT_PROMPT`

**Workflow**:
```
1. Call parse_requirements
2. Call classify_complexity
3. Call validate_requirements
4. Present final answer only after all 3 tools succeed
```

**Output**: Validated requirements with complexity classification and validation status.

### 2. Architecture Planner Sub-Agent

**Purpose**: Design Kubernetes architecture and estimate resources.

**Tools**:
1. `analyze_application_requirements` - Deep analysis of framework characteristics
2. `design_kubernetes_architecture` - Plan K8s resource structure
3. `estimate_resources` - Calculate CPU/memory requests and limits
4. `define_scaling_strategy` - Design HPA configuration
5. `check_dependencies` - Identify required charts and services

**Prompt**: `ARCHITECTURE_PLANNER_SUBAGENT_PROMPT`

**Workflow**:
```
1. analyze_application_requirements
2. design_kubernetes_architecture
3. estimate_resources
4. define_scaling_strategy
5. check_dependencies
```

**Output**: Complete architecture design with resource specifications, scaling configuration, and dependencies.

---

## Tools

### Parser Tools

Located in: `k8s_autopilot/core/agents/helm_generator/planner/tools/parser/`

#### `parse_requirements`

**Purpose**: Extract structured requirements from user input.

**Input**: 
- `user_query` from state
- `updated_user_requirements` (from HITL responses)
- `question_asked` (context for parsing)

**Process**:
1. Formats prompt with user requirements and Q&A context
2. Uses LLM with Pydantic parser to extract structured data
3. Returns `ParsedRequirements` schema

**Output Schema**: `ParsedRequirements`
- `app_type`: Application type (e.g., "api_service", "microservice")
- `framework`: Framework name (e.g., "fastapi", "express")
- `language`: Programming language (e.g., "python", "nodejs")
- `image`: Container image information
- `service`: Service access configuration
- `configuration`: Environment variables and secrets
- `resources`: CPU/memory requirements
- `namespace`: Namespace configuration
- `deployment`: Deployment configuration (replicas, HA, regions)
- `security`: Security requirements
- `databases`: Database requirements
- `external_services`: External service dependencies

**LLM Configuration**: Uses `higher_llm_config` for better extraction accuracy.

#### `classify_complexity`

**Purpose**: Assess deployment complexity level.

**Input**: Parsed requirements from `handoff_data`

**Process**:
1. Analyzes component count (app + databases + services)
2. Evaluates special features (HA, multi-region, security)
3. Classifies as simple/medium/complex

**Output Schema**: `ComplexityClassification`
- `complexity_level`: "simple" | "medium" | "complex"
- `reasoning`: Explanation of classification
- `components_count`: Total number of components
- `special_considerations`: List of complexity factors
- `requires_human_review`: Boolean flag

**Complexity Criteria**:
- **Simple**: Single component, stateless, basic config
- **Medium**: 2-3 components, some HA/security features
- **Complex**: 4+ components, advanced features, multiple K8s resources

#### `validate_requirements`

**Purpose**: Validate requirement completeness and correctness.

**Input**: 
- Parsed requirements
- Complexity classification
- Questions asked (to avoid duplicate questions)

**Process**:
1. Reviews "Questions Asked" to understand what was already requested
2. Checks if parsed requirements answer those questions
3. Validates critical fields are present
4. Generates **ONLY NEW** clarification questions (avoids duplicates)

**Output Schema**: `ValidationResult`
- `valid`: Boolean indicating if requirements are complete
- `missing_fields`: List of critical missing fields
- `clarifications_needed`: List of NEW questions (not previously asked)
- `validation_errors`: List of validation errors

**Critical Fields**:
- `app_type`: Required
- `image.repository` and `image.tag`: Required for container deployments
- `deployment.min_replicas`: Must be at least 1

**Defaulting Strategy**: Some fields can use defaults (resources, health checks, storage) and won't fail validation.

**HITL Integration**: If `clarifications_needed` is non-empty, `ValidateRequirementsHITLMiddleware` triggers interrupt.

### Analyzer Tools

Located in: `k8s_autopilot/core/agents/helm_generator/planner/tools/analyzer/`

#### `analyze_application_requirements`

**Purpose**: Deep technical analysis of application characteristics.

**Input**: 
- Parsed requirements
- User clarification transcript

**Process**:
1. Analyzes framework-specific metrics (startup time, memory, CPU)
2. Determines scalability characteristics
3. Evaluates storage requirements
4. Configures networking (port, protocol, TLS)
5. Generates Kubernetes-specific specifications

**Output Schema**: `ApplicationAnalysisOutput`
- `framework_analysis`: Startup time, memory, CPU, probes
- `scalability`: Horizontal scaling, stateless, HPA config
- `storage`: Temp and persistent storage needs
- `networking`: Port, protocol, TLS requirements
- `configuration`: ConfigMaps and Secrets needed
- `security`: Security context settings

**Key Features**:
- Framework-aware analysis (FastAPI vs Express vs Spring Boot)
- Conflict resolution: User clarification overrides initial requirements
- Sensible defaults for missing information

#### `design_kubernetes_architecture`

**Purpose**: Design complete Kubernetes resource architecture.

**Input**:
- Parsed requirements
- Application analysis
- User clarification transcript

**Process**:
1. Extracts critical configs from user clarification
2. Follows workload decision tree:
   - Stateless + horizontally scalable → Deployment + HPA
   - Requires stable identity → StatefulSet
   - Must run on every node → DaemonSet
   - One-time execution → Job
   - Scheduled → CronJob
3. Includes essential resources (Service, ConfigMap, Secret, etc.)
4. Applies security policies (NetworkPolicy, ServiceAccount, RBAC)

**Output Schema**: `KubernetesArchitectureOutput`
- `resources.core`: Core resources (Deployment/StatefulSet/etc.)
- `resources.auxiliary`: Supporting resources (Service, ConfigMap, HPA, etc.)
- `architecture_pattern`: Overall pattern (stateless_microservice, stateful_application, etc.)
- `estimated_complexity`: Operational complexity (low/medium/high)

**Decision Tree**:
```
Horizontally scalable AND stateless?
├─ YES → Deployment + HPA (if min < max replicas)
└─ NO → Requires stable identity OR persistent storage?
    ├─ YES → StatefulSet + Service (headless) + PVC
    └─ NO → Must run on every node?
        ├─ YES → DaemonSet + PDB
        └─ NO → One-time execution OR batch?
            ├─ YES (one-time) → Job
            ├─ YES (scheduled) → CronJob
            └─ NO → ERROR
```

**Essential Resources**:
- Namespace (if specified)
- Workload (Deployment/StatefulSet/etc.)
- Service (always required)
- ConfigMap (if env vars or config files)
- Secret (if secrets mentioned or TLS)
- PersistentVolumeClaim (if persistent storage)
- HPA (if scaling required)
- PodDisruptionBudget (if HA required)
- NetworkPolicy (if security required)
- ServiceAccount (if RBAC required)
- Ingress (if external HTTP access)

#### `estimate_resources`

**Purpose**: Estimate CPU and memory resources across environments.

**Input**:
- Parsed requirements
- Application analysis

**Process**:
1. Analyzes framework resource patterns (JVM heap, Node.js event loop)
2. Defines resources for dev/staging/prod:
   - **Dev**: Minimal resources, cost-efficient
   - **Staging**: Mirrors production scaled down (75-90%)
   - **Prod**: High availability with buffer (15-25% headroom)
3. Ensures requests ≤ limits
4. Provides technical reasoning

**Output Schema**: `ResourceEstimationOutput`
- `dev`: Resource specs for development
- `staging`: Resource specs for staging
- `prod`: Resource specs for production
- `reasoning`: Detailed explanation
- `framework_considerations`: Framework-specific factors
- `metadata`: Estimation methodology and confidence
- `cost_optimization_notes`: Cost optimization recommendations

**QoS Classes**:
- **Guaranteed**: Requests == Limits (both CPU and memory)
- **Burstable**: Requests < Limits (default for most apps)
- **BestEffort**: No requests or limits (dev only)

#### `define_scaling_strategy`

**Purpose**: Define HPA configuration for autoscaling.

**Input**:
- Parsed requirements
- Application analysis
- Scaling context (HPA/PDB config from architecture design)

**Process**:
1. Differentiates environments (dev/staging/prod)
2. Sets min/max replicas based on HA requirements
3. Configures CPU/memory thresholds
4. Defines scaling behavior (scale-up/down policies)
5. Configures PodDisruptionBudget

**Output Schema**: `ScalingStrategyOutput`
- `dev`: HPA config for development
- `staging`: HPA config for staging
- `prod`: HPA config for production
- `scaling_behavior`: Advanced scaling behavior (K8s 1.18+)
- `target_kind`: Target workload type
- `selector_labels`: Label selector for pods

**Environment Differentiation**:
- **Dev**: min=1, max=2-3, CPU threshold=80% (cost optimization)
- **Staging**: min=2, max=5-10, CPU threshold=70% (balanced)
- **Prod**: min≥3 (if HA), max=20-100+, CPU threshold=60-70% (responsive)

**High Availability Rules**:
- If `high_availability=true` OR `min_replicas >= 2`: Include PDB
- Prod min_replicas ≥ 3 for true HA (N+1 fault tolerance)
- Override user preferences if they violate safety rules

#### `check_dependencies`

**Purpose**: Identify Helm chart dependencies, init containers, sidecars, and hooks.

**Input**: Parsed requirements

**Process**:
1. Identifies Helm chart dependencies (PostgreSQL, Redis, RabbitMQ, etc.)
2. Determines init containers for pre-startup tasks
3. Identifies sidecar containers
4. Specifies Helm lifecycle hooks

**Output Schema**: `DependenciesOutput`
- `helm_dependencies`: List of Helm chart dependencies
- `init_containers_needed`: List of init containers
- `sidecars_needed`: List of sidecar containers
- `helm_hooks`: List of Helm hooks
- `dependency_rationale`: Detailed explanation
- `warnings`: Potential concerns or tradeoffs

**Helm Dependencies**:
- Uses Bitnami charts when available
- Pins versions (e.g., "12.x", "^1.0.0")
- Makes optional dependencies conditional

**Init Container Patterns**:
- Wait-for-service: Ensure dependencies ready
- Schema-migrate: Run database migrations
- Config-download: Fetch external configuration

**Sidecar Patterns**:
- Logging sidecar: Centralized log shipping
- Metrics exporter: Export custom metrics
- Secrets sync: Continuous vault synchronization

**Helm Hooks**:
- `pre-install`: Validate prerequisites
- `post-install`: Create default data, smoke tests
- `pre-upgrade`: Backup data, validate migration readiness
- `post-upgrade`: Run migrations, cache invalidation
- `pre-delete`: Backup data, graceful cleanup
- `post-delete`: External resource cleanup
- `test`: Helm test validation

---

## State Management

### State Schema: `PlanningSwarmState`

**Location**: `k8s_autopilot/core/state/base.py`

**Required Fields**:
- `messages`: Annotated[List[AnyMessage], add_messages] - Conversation history
- `remaining_steps`: Optional[int] - Required by Deep Agent TodoListMiddleware

**Optional Fields**:
- `user_query`: Initial user requirements
- `updated_user_requirements`: User responses to clarification questions
- `question_asked`: Questions asked via HITL
- `active_agent`: Currently active sub-agent
- `status`: Workflow status
- `session_id`: Session identifier
- `task_id`: Task identifier
- `handoff_data`: Data passed between tools (parsed requirements, analysis results, etc.)
- `chart_plan`: Final compiled chart plan
- `todos`: Todo list for next phases
- `workspace_files`: Workspace file artifacts
- `pending_feedback_requests`: HITL feedback requests
- `tool_call_results_for_review`: Tool call results for review
- `pending_tool_calls`: Pending tool calls requiring approval

**State Reducers**:
- `messages`: Uses `add_messages` for concurrent message updates
- `handoff_data`: Uses merge reducer `lambda x, y: {**(x or {}), **(y or {})}`
- `todos`: Uses `add` for list concatenation
- `workspace_files`: Uses merge reducer

**State Flow**:
```
1. Supervisor receives user_query
2. Extracts requirements → stores in handoff_data
3. Requests clarifications → updates updated_user_requirements
4. Delegates to requirements_analyzer → updates handoff_data
5. Delegates to architecture_planner → updates handoff_data
6. Compiles final plan → stores in chart_plan
```

---

## Workflow

### Complete Planning Workflow

```
User Query Received
    ↓
STEP 1: EXTRACT Available Details
    ├─ Parse query for 12 fields:
    │   - Application Type, Language, Framework
    │   - Container Image, Replicas, Resources
    │   - Exposure, Ingress Details
    │   - Config/Secrets, Persistent Storage
    │   - Health Checks, Namespace
    └─ Store extracted values in context
    ↓
STEP 2: DECIDE (Based on gaps)
    ├─ If ZERO gaps → Skip to Step 3
    │
    ├─ If 1-3 gaps → Execute Step 2A
    │   └─ Call request_human_input() with grouped questions
    │   └─ Wait for response
    │   └─ Merge with extracted data
    │
    └─ If 4+ gaps → Execute Step 2A (prioritized)
        └─ Call request_human_input() with grouped questions
        └─ Wait for response
        └─ Merge with extracted data
    ↓
STEP 3: Requirements Analysis (Delegation)
    └─ task(agent="requirements_analyzer", instructions="{final_requirements}")
        ├─ parse_requirements
        ├─ classify_complexity
        └─ validate_requirements
    ↓
STEP 4: Architecture Planning (Delegation)
    └─ task(agent="architecture_planner", instructions="{validated_requirements}")
        ├─ analyze_application_requirements
        ├─ design_kubernetes_architecture
        ├─ estimate_resources
        ├─ define_scaling_strategy
        └─ check_dependencies
    ↓
STEP 5: Compile & Save (Final)
    ├─ Aggregate all outputs
    ├─ Store in chart_plan
    ├─ Generate todos for generation phase
    └─ Complete
```

### Gap Detection Priority

**Priority 1 (CRITICAL)**:
1. Container Image - No deployment possible without it
2. Exposure/Access Method - How will it be accessed?

**Priority 2 (IMPORTANT)**:
3. Namespace - What namespace should it be deployed to?
4. Replicas & Resources - Scaling and performance requirements
5. Config/Secrets - Environment variables and sensitive data

**Priority 3 (OPTIONAL)**:
6. Persistent Storage - If app requires data persistence
7. Health Checks - For production-grade robustness

### Requirement Extraction Fields

| Field | Detection Keywords | Examples |
|-------|-------------------|----------|
| **Application Type** | "backend", "frontend", "API", "microservice" | "backend application", "REST API" |
| **Language/Runtime** | "Python", "Node.js", "Java", "Go" | "Python framework", "Node.js backend" |
| **Framework** | "FastAPI", "Django", "Express", "Spring Boot" | "developed in fastapi", "using Django REST" |
| **Container Image** | `registry/repo/image:tag` OR "Docker image" | "myrepo/api:v1.0", "docker.io/myapp:latest" |
| **Replicas** | "instances", "replicas", "pods", "copies", "HA" | "3 replicas", "2 instances for HA" |
| **Resources** | "CPU", "memory", "500m", "512Mi", "1Gi" | "500m CPU and 512Mi memory" |
| **Exposure** | "Ingress", "hostname", "domain", "LoadBalancer" | "Ingress at api.example.com" |
| **Ingress Details** | hostname, TLS, HTTPS, SSL, certificate | "Ingress at api.example.com with TLS" |
| **Config/Secrets** | "environment variables", "env vars", "secrets" | "env vars for DB connection" |
| **Persistent Storage** | "storage", "persistent volume", "database" | "needs persistent storage" |
| **Health Checks** | "/health", "/readiness", "/liveness", "probe" | "exposes /health endpoint" |
| **Namespace** | "namespace", "deploy to", "environment" | "deploy to myapp-prod" |

---

## Prompts

### Main Supervisor Prompt

**Location**: `k8s_autopilot/core/agents/helm_generator/planner/planning_prompts.py`

**Key**: `PLANNING_SUPERVISOR_PROMPT`

**Core Workflow**:
1. **EXTRACT**: Parse query for 12 fields
2. **DECIDE**: If gaps found → call `request_human_input`, else delegate
3. **COMPILE**: Aggregate outputs to `chart-plan.json`

**Gap Handling**:
- Groups all questions in ONE tool call
- Prioritizes critical fields (Image, Exposure)
- Acknowledges extracted info before asking

**Critical Rules**:
- **TOOLS ONLY**: Never write text responses, use `request_human_input` for questions
- **NO REDUNDANCY**: Never ask for details already provided
- **CONTEXTUAL**: Start questions with "✅ EXTRACTED: [Found items]"
- **SEQUENCE**: Clarify → Requirements Analysis → Architecture Planning → Compile

### Requirements Analyzer Prompt

**Key**: `REQUIREMENT_ANALYZER_SUBAGENT_PROMPT`

**Workflow**:
1. Call `parse_requirements`
2. Call `classify_complexity`
3. Call `validate_requirements`
4. Present final answer only after all 3 tools succeed

**Key Rules**:
- Always follow the sequence above
- If `validate_requirements` returns `valid=false`, ask specific clarification questions
- Do NOT proceed to final answer until validation passes

### Architecture Planner Prompt

**Key**: `ARCHITECTURE_PLANNER_SUBAGENT_PROMPT`

**Workflow**:
1. `analyze_application_requirements` - Deep app understanding
2. `design_kubernetes_architecture` - Plan resource structure
3. `estimate_resources` - Sizing recommendations
4. `define_scaling_strategy` - HPA configuration
5. `check_dependencies` - Identify required charts and services

**Output Format**:
- Kubernetes Resources (list with justification)
- Resource Sizing (CPU/memory requests/limits with reasoning)
- Scaling Configuration (HPA settings)
- Dependencies (Helm chart dependencies, init containers, sidecars)
- Best Practices Applied (Bitnami compliance, security hardening)

### Tool Prompts

#### Parser Tool Prompts

**Location**: `k8s_autopilot/core/agents/helm_generator/planner/tools/parser/req_parser_prompts.py`

- `REQUIREMENT_PARSER_SYSTEM_PROMPT`: Instructions for extracting structured requirements
- `REQUIREMENT_PARSER_USER_PROMPT`: Template for user requirements input
- `CLASSIFY_COMPLEXITY_SYSTEM_PROMPT`: Instructions for complexity classification
- `CLASSIFY_COMPLEXITY_USER_PROMPT`: Template for complexity analysis
- `VALIDATE_REQUIREMENTS_SYSTEM_PROMPT`: Instructions for validation (with question avoidance strategy)
- `VALIDATE_REQUIREMENTS_USER_PROMPT`: Template for validation input

**Key Features**:
- **Question Avoidance Strategy**: Reviews "Questions Asked" to avoid duplicate questions
- **Extraction Precedence**: Additional Requirements override User Requirements
- **Inference Rules**: Provides defaults for replicas based on environment
- **Format Normalization**: Normalizes CPU/memory/image formats

#### Analyzer Tool Prompts

**Location**: `k8s_autopilot/core/agents/helm_generator/planner/tools/analyzer/planner_analyzer_prompts.py`

- `ANALYZE_APPLICATION_REQUIREMENTS_SYSTEM_PROMPT`: Framework-specific analysis instructions
- `ANALYZE_APPLICATION_REQUIREMENTS_HUMAN_PROMPT`: Template for application analysis
- `DESIGN_KUBERNETES_ARCHITECTURE_SYSTEM_PROMPT`: Architecture design instructions with decision tree
- `DESIGN_KUBERNETES_ARCHITECTURE_HUMAN_PROMPT`: Template for architecture design
- `ESTIMATE_RESOURCES_SYSTEM_PROMPT`: Resource estimation guidelines
- `ESTIMATE_RESOURCES_HUMAN_PROMPT`: Template for resource estimation
- `DEFINE_SCALING_STRATEGY_SYSTEM_PROMPT`: HPA configuration instructions
- `DEFINE_SCALING_STRATEGY_HUMAN_PROMPT`: Template for scaling strategy
- `CHECK_DEPENDENCIES_SYSTEM_PROMPT`: Dependency analysis instructions
- `CHECK_DEPENDENCIES_HUMAN_PROMPT`: Template for dependency checking

**Key Features**:
- **Decision Trees**: Structured decision-making for workload selection
- **Environment Differentiation**: Different strategies for dev/staging/prod
- **Conflict Resolution**: User clarification overrides initial requirements
- **Best Practices**: Bitnami compliance, security hardening, HA considerations

---

## Integration

### Integration with Main Supervisor

The Planner Agent is integrated into the main supervisor workflow via a tool:

**Location**: `k8s_autopilot/core/agents/supervisor_agent.py`

**Tool**: `transfer_to_planning_swarm`

```python
@tool
async def transfer_to_planning_swarm(
    task_description: str,
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Delegate to planning swarm for Helm chart architecture planning.
    
    Use this when:
    - User requests to create/write/generate Helm charts
    - Need to analyze requirements and create execution plans
    - workflow_state.planning_complete == False
    - active_phase == "requirements"
    """
```

**State Transformation**:
1. **Supervisor → Planning**: `StateTransformer.supervisor_to_planning()`
   - Maps `user_query` → `user_query`
   - Maps `session_id`, `task_id`
   - Initializes `PlanningSwarmState`

2. **Planning → Supervisor**: `StateTransformer.planning_to_supervisor()`
   - Maps `chart_plan` → `planner_output`
   - Updates `workflow_state.planning_complete = True`
   - Updates `active_phase = "generation"`

### Factory Function

**Location**: `k8s_autopilot/core/agents/helm_generator/planner/planning_swarm.py`

```python
def create_planning_swarm_deep_agent(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "planning_swarm_deep_agent",
    memory: Optional[MemorySaver] = None
) -> k8sAutopilotPlanningDeepAgent
```

**Usage**:
```python
planning_swarm_deep_agent = create_planning_swarm_deep_agent(config)
compiled_planning_swarm = planning_swarm_deep_agent.build_graph().compile()
```

---

## Examples

### Example 1: Partial Input (Gaps Detected)

**User Input**:
```
I am going to deploy my backend application totally developed in fastapi python framework. 
So can you help me in writing helm chart for this application.
```

**Extraction Result**:
```
✅ Application Type: Backend application
✅ Language: Python
✅ Framework: FastAPI
❌ Container Image: MISSING
❌ Replicas: MISSING
❌ Resources: MISSING
❌ Exposure: MISSING
❌ Namespace: MISSING
❌ Config/Secrets: MISSING
❌ Persistent Storage: MISSING
❌ Health Checks: MISSING
```

**Agent Action**: Calls `request_human_input()` with grouped questions:

```python
request_human_input(
  question="""✅ EXTRACTED: Perfect! I can see you're deploying a **FastAPI backend application**. 

❓ TO PROCEED, I need a few more specifics:

1. **Container Image** (Required)
   Do you have a Docker image ready? If so, what's the repository, image name, and tag?
   Example: docker.io/myuser/fastapi-app:v1.0

2. **Deployment Scale & Resources**
   How many replicas should run? (For high availability, 2-3 recommended)
   What CPU and memory per replica? (e.g., 250m CPU, 256Mi memory)

3. **Access Method** (How will clients reach your app?)
   Should it be exposed via:
   - Ingress (Note: We use **Traefik** for traffic management)
   - LoadBalancer
   - NodePort
   - ClusterIP (internal only)
   
   If Ingress: What hostname? Do you need HTTPS/TLS?

4. **Namespace & Environment** (Important)
   Which namespace should this be deployed to?
   - Existing namespace name (e.g., `myapp-prod`, `backend-staging`)
   - Or should we create a new one? What environment type? (production/staging/development)
   - Which team owns this namespace? (e.g., backend, platform, devops)

5. **Configuration & Secrets** (Optional but recommended)
   Does your FastAPI app need any environment variables or secrets?
   (e.g., database connection strings, API keys, JWT secrets)

6. **Storage & Health Checks** (Optional, can use defaults)
   Does your app need persistent storage?
   Does your app expose a /health or /readiness endpoint?""",
  context="Missing 8 field(s) for Helm chart planning: image, replicas, resources, exposure, namespace, config, storage, health_checks",
  phase="planning"
)
```

**After User Response**: Merges feedback into `updated_user_requirements` and proceeds to requirements analysis.

### Example 2: Complete Input (No Gaps)

**User Input**:
```
Deploy my FastAPI app, image: docker.io/mycompany/api:v2.1, 
3 replicas, 500m CPU and 512Mi memory, Ingress at api.example.com with TLS, 
namespace: myapp-prod, needs env vars for DB_HOST and DB_USER.
```

**Extraction Result**:
```
✅ Type: Backend (API)
✅ Language: Python
✅ Framework: FastAPI
✅ Image: docker.io/mycompany/api:v2.1
✅ Replicas: 3
✅ Resources: 500m CPU, 512Mi memory
✅ Exposure: Ingress
✅ Ingress Details: api.example.com with TLS
✅ Namespace: myapp-prod
✅ Config/Secrets: DB_HOST, DB_USER env vars
```

**Agent Action**: Skips questions, directly delegates to requirements analyzer:

```python
task(
  agent="requirements_analyzer", 
  instructions="Validate FastAPI app deployment: image docker.io/mycompany/api:v2.1, 3 replicas, resources 500m/512Mi, Ingress api.example.com with TLS, namespace myapp-prod, requires DB_HOST and DB_USER env vars."
)
```

### Example 3: Tool Execution Flow

**Requirements Analyzer Execution**:

```python
# Step 1: Parse requirements
parse_requirements() → {
  "parsed_requirements": {
    "app_type": "api_service",
    "framework": "fastapi",
    "language": "python",
    "image": {"repository": "docker.io/mycompany/api", "tag": "v2.1"},
    "deployment": {"min_replicas": 3, "max_replicas": 10},
    ...
  }
}

# Step 2: Classify complexity
classify_complexity() → {
  "complexity_classification": {
    "complexity_level": "medium",
    "components_count": 1,
    "reasoning": "Single application component with moderate features...",
    ...
  }
}

# Step 3: Validate requirements
validate_requirements() → {
  "validation_result": {
    "valid": true,
    "missing_fields": [],
    "clarifications_needed": [],
    ...
  }
}
```

**Architecture Planner Execution**:

```python
# Step 1: Analyze application requirements
analyze_application_requirements() → {
  "application_analysis": {
    "framework_analysis": {
      "startup_time_seconds": 30,
      "typical_memory_mb": 256,
      "cpu_cores": 0.5,
      ...
    },
    "scalability": {
      "horizontally_scalable": true,
      "stateless": true,
      "hpa_enabled": true,
      ...
    },
    ...
  }
}

# Step 2: Design Kubernetes architecture
design_kubernetes_architecture() → {
  "kubernetes_architecture": {
    "resources": {
      "core": [{"type": "Deployment", ...}],
      "auxiliary": [
        {"type": "Service", ...},
        {"type": "Ingress", ...},
        {"type": "HorizontalPodAutoscaler", ...},
        ...
      ],
      ...
    },
    ...
  }
}

# Step 3: Estimate resources
estimate_resources() → {
  "resource_estimation": {
    "dev": {"requests": {"cpu": "250m", "memory": "256Mi"}, ...},
    "staging": {"requests": {"cpu": "400m", "memory": "400Mi"}, ...},
    "prod": {"requests": {"cpu": "500m", "memory": "512Mi"}, ...},
    ...
  }
}

# Step 4: Define scaling strategy
define_scaling_strategy() → {
  "scaling_strategy": {
    "dev": {"min_replicas": 1, "max_replicas": 3, "target_cpu_utilization": 80},
    "staging": {"min_replicas": 2, "max_replicas": 5, "target_cpu_utilization": 70},
    "prod": {"min_replicas": 3, "max_replicas": 20, "target_cpu_utilization": 65},
    ...
  }
}

# Step 5: Check dependencies
check_dependencies() → {
  "dependencies": {
    "helm_dependencies": [],
    "init_containers_needed": [],
    "sidecars_needed": [],
    "helm_hooks": [],
    ...
  }
}
```

### Example 4: Final Chart Plan Structure

**Final Output** (`chart_plan` in state):

```json
{
  "parsed_requirements": {
    "app_type": "api_service",
    "framework": "fastapi",
    "language": "python",
    "image": {"repository": "docker.io/mycompany/api", "tag": "v2.1"},
    "deployment": {"min_replicas": 3, "max_replicas": 10},
    "service": {"access_type": "ingress", "port": 80},
    "namespace": {"name": "myapp-prod", "namespace_type": "production"},
    "configuration": {
      "environment_variables": [
        {"name": "DB_HOST", "from_configmap": true, "value": "app-config", "key": "DB_HOST"},
        {"name": "DB_USER", "from_secret": true, "value": "app-secrets", "key": "DB_USER"}
      ]
    },
    "resources": {"cpu_request": "500m", "memory_request": "512Mi"}
  },
  "complexity_classification": {
    "complexity_level": "medium",
    "components_count": 1,
    "requires_human_review": false
  },
  "validation_result": {
    "valid": true,
    "missing_fields": [],
    "clarifications_needed": []
  },
  "application_analysis": {
    "framework_analysis": {...},
    "scalability": {...},
    "storage": {...},
    "networking": {...},
    "configuration": {...},
    "security": {...}
  },
  "kubernetes_architecture": {
    "resources": {
      "core": [{"type": "Deployment", ...}],
      "auxiliary": [
        {"type": "Service", ...},
        {"type": "Ingress", ...},
        {"type": "HorizontalPodAutoscaler", ...},
        {"type": "ConfigMap", ...},
        {"type": "Secret", ...}
      ]
    }
  },
  "resource_estimation": {
    "dev": {...},
    "staging": {...},
    "prod": {...}
  },
  "scaling_strategy": {
    "dev": {...},
    "staging": {...},
    "prod": {...}
  },
  "dependencies": {
    "helm_dependencies": [],
    "init_containers_needed": [],
    "sidecars_needed": [],
    "helm_hooks": []
  }
}
```

---

## Best Practices

### For Developers

1. **State Management**: Always use proper reducers for concurrent state updates
2. **Tool Design**: Tools should return `Command` with state updates, not direct values
3. **Error Handling**: Tools should catch exceptions and return error messages via `ToolMessage`
4. **HITL Integration**: Use `request_human_input` tool, never write questions as text
5. **Prompt Engineering**: Reference specific input values in prompts for better accuracy

### For Users

1. **Provide Complete Information**: Include container image, exposure method, and namespace upfront
2. **Be Specific**: Specify exact resource requirements (CPU/memory) when possible
3. **Answer Clarifications**: Respond to clarification questions promptly and completely
4. **Review Output**: Check the final `chart_plan` before proceeding to generation phase

### For Operators

1. **Monitor State**: Track `handoff_data` to understand planning progress
2. **Check Logs**: Review structured logs for debugging planning issues
3. **Validate Output**: Ensure `chart_plan` contains all required fields before generation
4. **Handle Errors**: Retry failed tools with updated inputs

---

## Troubleshooting

### Common Issues

1. **Missing State Fields**: Ensure `PlanningStateMiddleware` is configured correctly
2. **Duplicate Questions**: Check that `validate_requirements` reviews "Questions Asked"
3. **Tool Failures**: Review tool error messages and retry with corrected inputs
4. **State Updates Not Persisting**: Verify checkpointer is configured and state reducers are correct
5. **HITL Not Triggering**: Ensure `request_human_input` tool is used, not text output

### Debugging Tips

1. **Enable Debug Logging**: Set log level to DEBUG for detailed tool execution logs
2. **Inspect State**: Check `handoff_data` to see intermediate tool outputs
3. **Review Prompts**: Verify prompts are correctly formatted and include all context
4. **Check LLM Configuration**: Ensure LLM models are correctly configured and accessible

---

## References

- **Implementation**: `k8s_autopilot/core/agents/helm_generator/planner/planning_swarm.py`
- **Parser Tools**: `k8s_autopilot/core/agents/helm_generator/planner/tools/parser/`
- **Analyzer Tools**: `k8s_autopilot/core/agents/helm_generator/planner/tools/analyzer/`
- **Prompts**: `k8s_autopilot/core/agents/helm_generator/planner/planning_prompts.py`
- **State Schema**: `k8s_autopilot/core/state/base.py` (PlanningSwarmState)
- **Integration**: `k8s_autopilot/core/agents/supervisor_agent.py`

---

**Version**: 1.0  
**Last Updated**: 2025-01-XX  
**Status**: Production-Ready
