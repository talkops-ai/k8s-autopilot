# Supervisor Agent - Comprehensive Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Implementation Details](#implementation-details)
4. [State Management](#state-management)
5. [State Transformation](#state-transformation)
6. [Human-in-the-Loop (HITL) Gates](#human-in-the-loop-hitl-gates)
7. [Tool-Based Delegation](#tool-based-delegation)
8. [Workflow Orchestration](#workflow-orchestration)
9. [Stream Processing](#stream-processing)
10. [Interrupt Handling](#interrupt-handling)
11. [Integration with Swarms](#integration-with-swarms)
12. [Best Practices](#best-practices)

---

## Overview

The **Supervisor Agent** (`k8sAutopilotSupervisorAgent`) is the central orchestrator for the k8s-autopilot Helm chart generation workflow. It coordinates specialized agent swarms, manages human-in-the-loop approvals, and ensures proper state flow between phases.

### Key Responsibilities

- ✅ **Workflow Orchestration**: Coordinates phases: planning → generation → validation
- ✅ **Agent Delegation**: Routes tasks to specialized swarms via tool-based delegation
- ✅ **State Management**: Maintains `MainSupervisorState` and transforms state between swarms
- ✅ **Human-in-the-Loop**: Manages approval gates at critical workflow points
- ✅ **Error Handling**: Detects and handles workflow errors and completion
- ✅ **Stream Processing**: Provides async streaming with interrupt detection

### Current Scope

**⚠️ Important**: Currently, the Supervisor Agent **ONLY supports Helm chart generation**. While the architecture is designed to support broader Kubernetes operations (deployment, CI/CD, etc.), the current implementation focuses exclusively on:

- Helm chart planning
- Helm chart template generation
- Helm chart validation

Future releases will expand capabilities to include deployment, CI/CD pipelines, and other Kubernetes operations.

---

## Architecture

### High-Level Pattern: Tool-Based Delegation

The Supervisor Agent uses **LangChain's `create_agent()` pattern** with tool wrappers instead of manual StateGraph building. This simplifies orchestration and allows the LLM to decide routing dynamically.

```
┌─────────────────────────────────────────────────────────────┐
│              Supervisor Agent (create_agent)                 │
│  - LLM Model (GPT-4/Claude)                                  │
│  - System Prompt (workflow instructions)                     │
│  - Tools (swarm delegation + HITL gates)                    │
│  - Checkpointer (PostgreSQL/Memory)                          │
└─────────────┬───────────────────────────────────────────────┘
              │
              │ Tool Calls (dynamic routing)
              │
    ┌─────────┴─────────┬──────────────┬──────────────────┐
    │                   │              │                  │
    ▼                   ▼              ▼                  ▼
┌─────────┐      ┌──────────┐   ┌──────────┐      ┌──────────┐
│Planning │      │Template  │   │Validator │      │HITL Gates│
│ Swarm   │      │Supervisor│   │Deep Agent│      │          │
│         │      │          │   │          │      │          │
│(Deep    │      │(LangGraph│   │(Deep     │      │(Interrupt│
│ Agent)  │      │StateGraph)│   │Agent)    │      │ Tools)   │
└─────────┘      └──────────┘   └──────────┘      └──────────┘
```

### Architecture Components

#### 1. **Supervisor Core** (`k8sAutopilotSupervisorAgent`)

- **Pattern**: Uses `create_agent()` from LangChain
- **LLM**: Configurable via `LLMProvider` (GPT-4, Claude, etc.)
- **Tools**: Dynamic tool wrappers for swarm delegation
- **State Schema**: `MainSupervisorState` (TypedDict with reducers)
- **Checkpointer**: PostgreSQL (preferred) or MemorySaver (fallback)

#### 2. **Tool Wrappers**

Each swarm is wrapped in a `@tool` function that:
- Transforms supervisor state → swarm state
- Invokes the swarm subgraph
- Transforms swarm results → supervisor state updates
- Returns `Command` with state updates

#### 3. **HITL Gate Tools**

Special tools that trigger interrupts for human approval:
- `request_planning_review`: Review planning output
- `request_generation_review`: Review generated artifacts + workspace selection

#### 4. **State Transformer**

`StateTransformer` class handles bidirectional state conversion:
- `supervisor_to_planning()` / `planning_to_supervisor()`
- `supervisor_to_generation()` / `generation_to_supervisor()`
- `supervisor_to_validation()` / `validation_to_supervisor()`

---

## Implementation Details

### Class: `k8sAutopilotSupervisorAgent`

**Location**: `k8s_autopilot/core/agents/supervisor_agent.py`

**Inheritance**: `BaseAgent`

**Key Methods**:

```python
class k8sAutopilotSupervisorAgent(BaseAgent):
    def __init__(
        self,
        agents: List[BaseSubgraphAgent],
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        prompt_template: Optional[str] = None,
        name: str = "supervisor-agent"
    ):
        """
        Initialize the supervisor agent.
        
        Args:
            agents: List of BaseSubgraphAgent instances (planning, template, validator)
            config: Configuration object
            custom_config: Custom configuration dictionary
            prompt_template: Custom prompt template
            name: Agent name for identification
        """
```

**Initialization Steps**:

1. **Config Setup**: Uses centralized `Config` system
2. **Checkpointer**: Attempts PostgreSQL checkpointer, falls back to MemorySaver
3. **LLM Model**: Creates LLM via `LLMProvider.create_llm()`
4. **Agent Registration**: Registers subgraph agents (`planning_swarm`, `template_supervisor`, `validator_deep_agent`)
5. **Prompt Definition**: Sets default prompt or uses custom template
6. **Graph Building**: Builds supervisor graph using `_build_supervisor_graph()`

### Method: `_build_supervisor_graph()`

**Purpose**: Creates the supervisor agent using `create_agent()` with tool wrappers

**Implementation Flow**:

```python
def _build_supervisor_graph(self):
    # 1. Build compiled subgraphs for each swarm
    compiled_swarms = {}
    for agent_name, agent in self.agents.items():
        graph = agent.build_graph()
        # Compile if needed
        if hasattr(graph, 'compile'):
            compiled_graph = graph.compile(name=agent_name)
        else:
            compiled_graph = graph  # Already compiled
        compiled_swarms[agent_name] = compiled_graph
    
    # 2. Create tool wrappers for delegation
    swarm_tools = self._create_swarm_tools(compiled_swarms)
    
    # 3. Create HITL gate tools
    hitl_gate_tools = self._create_hitl_gate_tools()
    
    # 4. Create human feedback tool
    feedback_tool = self.request_human_feedback
    
    # 5. Combine all tools
    all_tools = swarm_tools + hitl_gate_tools + [feedback_tool]
    
    # 6. Create agent using create_agent()
    supervisor_agent = create_agent(
        model=self.model,
        tools=all_tools,
        system_prompt=self.prompt_template,
        state_schema=MainSupervisorState,
        checkpointer=self.memory
    )
    
    return supervisor_agent
```

**Key Points**:

- **No Manual Graph Building**: Uses `create_agent()` instead of manual `StateGraph`
- **Tool-Based Routing**: LLM decides which tool to call based on workflow state
- **Dynamic Delegation**: Tools handle state transformation and swarm invocation
- **HITL Integration**: Gate tools trigger interrupts for human approval

---

## State Management

### State Schema: `MainSupervisorState`

**Location**: `k8s_autopilot/core/state/base.py`

**Definition**:

```python
class MainSupervisorState(AgentState):
    """State schema for the main supervisor agent."""
    
    # Message history
    messages: Annotated[List[AnyMessage], add_messages]
    llm_input_messages: Annotated[List[AnyMessage], add_messages]
    
    # Deep Agent compatibility
    remaining_steps: Annotated[Optional[int], lambda x, y: y]
    
    # Core workflow data
    user_query: NotRequired[str]
    user_requirements: NotRequired[ChartRequirements]
    active_phase: NotRequired[Literal[
        "requirements", 
        "planning", 
        "generation", 
        "validation", 
        "error"
    ]]
    
    # Phase outputs
    planner_output: NotRequired[Dict[str, Any]]
    helm_chart_artifacts: NotRequired[Dict[str, str]]  # filepath -> content
    validation_results: Annotated[List[ValidationResult], add]
    
    # HITL tracking
    human_approval_status: NotRequired[Dict[str, ApprovalStatus]]
    pending_feedback_requests: NotRequired[Dict[str, Any]]
    tool_call_results_for_review: NotRequired[Dict[str, Any]]
    
    # Workflow state tracking
    workflow_state: NotRequired[SupervisorWorkflowState]
    
    # Session tracking
    session_id: NotRequired[str]
    task_id: NotRequired[str]
    
    # File artifacts
    file_artifacts: NotRequired[Dict[str, str]]
    todos: NotRequired[Annotated[List[Dict], add]]
    
    # Workspace configuration
    workspace_dir: NotRequired[str]  # Set during generation review
```

### Workflow State: `SupervisorWorkflowState`

**Purpose**: Tracks workflow progress and phase completion

**Key Fields**:

```python
class SupervisorWorkflowState(BaseModel):
    # Current phase
    current_phase: Literal[
        "requirements", "planning", "generation", 
        "validation", "error", "complete"
    ]
    
    # Phase completion flags
    planning_complete: bool
    generation_complete: bool
    validation_complete: bool
    
    # HITL approval flags
    planning_approved: bool
    generation_approved: bool
    
    # Workflow control
    workflow_complete: bool
    loop_counter: int  # Prevents infinite loops
    last_phase_transition: Optional[datetime]
    
    # Agent/swarm tracking
    last_swarm: Optional[str]
    next_swarm: Optional[str]
    handoff_reason: Optional[str]
```

**Helper Methods**:

- `set_phase_complete(phase: str)`: Marks a phase as complete
- `set_approval(approval_type: str, approved: bool)`: Sets approval status
- `increment_loop_counter()`: Prevents infinite loops
- `is_complete`: Property checking if all phases are complete

### State Reducers

#### 1. `add_messages` (Messages)

**Purpose**: Accumulates messages in conversation history

**Usage**: All tool messages and LLM responses are added to `messages` list

#### 2. `add` (Validation Results, Todos)

**Purpose**: Accumulates validation results and todos

**Usage**: 
```python
validation_results: Annotated[List[ValidationResult], add]
todos: Annotated[List[Dict], add]
```

**Behavior**: New items are appended to existing list (never overwritten)

#### 3. Merge Reducer (File Artifacts)

**Purpose**: Merges file artifact dictionaries

**Usage**:
```python
file_artifacts: Annotated[Dict[str, str], lambda x, y: {**x, **y}]
```

**Behavior**: Merges dictionaries, with new values overriding old ones

---

## State Transformation

### Overview

The `StateTransformer` class handles bidirectional state conversion between supervisor and swarm states. Each transformation:

1. **Extracts** relevant data from supervisor state
2. **Transforms** to swarm-specific state schema
3. **Invokes** swarm subgraph
4. **Transforms** swarm results back to supervisor state updates

### Transformation Methods

#### 1. Planning Swarm Transformations

**Supervisor → Planning**:

```python
@staticmethod
def supervisor_to_planning(supervisor_state: MainSupervisorState) -> Dict:
    """
    Transform supervisor state to planning swarm input.
    
    Maps:
    - user_query → messages (HumanMessage)
    - Creates empty planning state fields
    """
    return {
        "messages": [HumanMessage(content=supervisor_state["user_query"])],
        "remaining_steps": None,  # Required by Deep Agent
        "active_agent": "requirement_analyzer",
        "chart_plan": None,
        "status": status_value,
        "todos": [],
        "workspace_files": {},
        "handoff_metadata": {},
        "session_id": supervisor_state.get("session_id"),
        "task_id": supervisor_state.get("task_id"),
        "user_query": supervisor_state.get("user_query")
    }
```

**Planning → Supervisor**:

```python
@staticmethod
def planning_to_supervisor(
    planning_state: PlanningSwarmState,
    original_supervisor_state: MainSupervisorState,
    tool_call_id: str
) -> Dict:
    """
    Transform planning swarm output back to supervisor state.
    
    Maps:
    - chart_plan → planner_output
    - Updates workflow_state.planning_complete
    - Creates summary messages
    """
    # Update workflow state
    workflow_state_obj.set_phase_complete("planning")
    
    # Create summary messages
    summary_messages = [
        ToolMessage(
            content=f"Planning swarm completed successfully...",
            tool_call_id=tool_call_id
        ),
        HumanMessage(content="Planning is complete. Please proceed...")
    ]
    
    return {
        "messages": summary_messages,
        "llm_input_messages": summary_messages,
        "planner_output": planning_state.get("chart_plan"),
        "workflow_state": updated_workflow_state
    }
```

#### 2. Generation Swarm Transformations

**Supervisor → Generation**:

```python
@staticmethod
def supervisor_to_generation(supervisor_state: MainSupervisorState) -> Dict:
    """
    Transform supervisor state to generation swarm input.
    
    Maps:
    - planner_output → planner_output (ChartPlan)
    - Updates workflow_state.current_phase = "generation"
    """
    return {
        "messages": [HumanMessage(content=supervisor_state["user_query"])],
        "planner_output": supervisor_state.get("planner_output"),
        "workflow_state": workflow_state_obj,
        "generated_templates": {},
        "validation_results": [],
        "template_variables": [],
        "session_id": supervisor_state.get("session_id"),
        "task_id": supervisor_state.get("task_id"),
        "generation_status": {}
    }
```

**Generation → Supervisor**:

```python
@staticmethod
def generation_to_supervisor(
    generation_state: GenerationSwarmState,
    original_supervisor_state: MainSupervisorState,
    tool_call_id: str
) -> Dict:
    """
    Transform generation swarm output back to supervisor state.
    
    Maps:
    - final_helm_chart → helm_chart_artifacts
    - Updates workflow_state.generation_complete
    """
    workflow_state_obj.set_phase_complete("generation")
    
    return {
        "messages": summary_messages,
        "llm_input_messages": summary_messages,
        "helm_chart_artifacts": generation_state.get("final_helm_chart"),
        "workflow_state": updated_workflow_state
    }
```

#### 3. Validation Swarm Transformations

**Supervisor → Validation**:

```python
@staticmethod
def supervisor_to_validation(
    supervisor_state: MainSupervisorState, 
    workspace_dir: str = "/tmp/helm-charts"
) -> Dict:
    """
    Transform supervisor state to validation swarm input.
    
    Maps:
    - helm_chart_artifacts → generated_chart
    - planner_output → chart_metadata
    - Pre-writes chart files to filesystem
    """
    generated_chart = supervisor_state.get("helm_chart_artifacts", {})
    
    # Extract chart name
    chart_name = extract_chart_name(generated_chart)
    chart_path = f"{workspace_dir}/{chart_name}"
    
    # Pre-write chart files to filesystem
    # (avoids context overload in messages)
    write_chart_files(generated_chart, chart_path)
    
    instruction = f"""Validate the Helm chart located at: {chart_path}
    ..."""
    
    return {
        "messages": [HumanMessage(content=instruction)],
        "active_agent": "validation_supervisor",
        "generated_chart": generated_chart,
        "validation_results": [],
        "blocking_issues": [],
        "handoff_metadata": {
            "chart_name": chart_name,
            "chart_path": chart_path,
            "workspace_dir": workspace_dir
        }
    }
```

**Validation → Supervisor**:

```python
@staticmethod
def validation_to_supervisor(
    validation_state: ValidationSwarmState,
    original_supervisor_state: MainSupervisorState,
    tool_call_id: str = "validation_complete"
) -> Dict:
    """
    Transform validation swarm output back to supervisor state.
    
    Maps:
    - validation_results → validation_results
    - blocking_issues → blocking_issues
    - Updates workflow_state.validation_complete (only on success)
    """
    # Only mark complete if no failures
    if phase_completed_successfully:
        workflow_state_obj.set_phase_complete("validation")
    
    return {
        "messages": summary_messages,
        "llm_input_messages": summary_messages,
        "validation_results": validation_results,
        "blocking_issues": blocking_issues,
        "workflow_state": updated_workflow_state
    }
```

### Pre-Writing Chart Files

**Important**: The `supervisor_to_validation()` transformation **pre-writes chart files** to the filesystem before invoking the validator. This:

- **Avoids context overload**: Chart contents aren't included in messages
- **Enables file-based tools**: Validator can use `ls`, `read_file`, etc.
- **Improves performance**: Files are written once, not repeatedly

**Implementation**:

```python
# Pre-write chart files to filesystem
chart_path = f"{workspace_dir}/{chart_name}"
files_written = []

for filename, content in generated_chart.items():
    file_path = f"{chart_path}/{filename}"
    # Handle templates/ subdirectory
    if filename.startswith("templates/"):
        template_name = filename.replace("templates/", "")
        file_path = f"{chart_path}/templates/{template_name}"
    
    # Create parent directories
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    # Write file
    with open(file_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    files_written.append(filename)
```

---

## Human-in-the-Loop (HITL) Gates

### Overview

HITL gates are **mandatory approval points** in the workflow where human review is required before proceeding. The Supervisor Agent uses **interrupt-based gates** that pause execution and wait for human input.

### Gate Types

#### 1. Planning Review Gate

**Tool**: `request_planning_review`

**When Called**: After planning phase completes (`workflow_state.planning_complete == True`)

**Purpose**: Review and approve the chart plan before generation

**Implementation**:

```python
@tool
async def request_planning_review(
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Request human review and approval of the planning output.
    """
    # Check if already approved
    if is_approved(runtime.state, "planning"):
        return Command(update={"messages": [...]})
    
    # Call gate function (triggers interrupt)
    gate_result = planning_review_gate(runtime.state)
    
    # Update approval status
    update_dict = {
        "human_approval_status": merged_approvals,
        "messages": [tool_message]
    }
    
    return Command(update=update_dict)
```

**Gate Function** (`planning_review_gate`):

```python
def planning_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    HITL gate for planning review.
    """
    # Check if already approved
    if is_approved(state, "planning"):
        return {"messages": [...]}
    
    # Extract planning summary
    planning_output = state.get("planning_output")
    summary = extract_planning_summary(planning_output)
    
    # Build review data
    review_data = format_review_data(
        phase="planning",
        summary=summary,
        data={"chart_plan": planning_output},
        required_action="approve",
        options=["approve", "reject", "modify"]
    )
    
    # Trigger interrupt - execution pauses here
    human_decision = interrupt(review_data)
    
    # Process decision
    updated_approvals = update_approval_status(
        state=state,
        approval_type="planning",
        decision=human_decision.get("decision"),
        reviewer=human_decision.get("reviewer"),
        comments=human_decision.get("comments")
    )
    
    return {
        "human_approval_status": updated_approvals,
        "messages": [...]
    }
```

**Interrupt Payload Structure**:

```python
{
    "phase": "planning",
    "summary": "Chart plan summary text...",
    "data": {
        "chart_plan": {...},
        "chart_info": {
            "name": "my-app",
            "version": "1.0.0"
        }
    },
    "required_action": "approve",
    "options": ["approve", "reject", "modify"]
}
```

#### 2. Generation Review Gate

**Tool**: `request_generation_review`

**When Called**: After generation phase completes (`workflow_state.generation_complete == True`)

**Purpose**: Review generated artifacts and specify workspace directory

**Key Features**:

- **Artifact Review**: Shows list of generated Helm chart files
- **Workspace Selection**: Requests workspace directory for validation
- **Approval Required**: Must approve before validation can proceed

**Implementation**:

```python
@tool
async def request_generation_review(
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Request human review of generated artifacts and obtain workspace directory.
    """
    # Check if already approved
    if is_approved(runtime.state, "generation"):
        return Command(update={"messages": [...]})
    
    # Call gate function
    gate_result = generation_review_gate(runtime.state)
    
    # Extract workspace_dir from human decision
    workspace_dir = gate_result.get("workspace_dir", "/tmp/helm-charts")
    
    update_dict = {
        "human_approval_status": merged_approvals,
        "workspace_dir": workspace_dir,  # CRITICAL: Set workspace_dir
        "messages": [tool_message]
    }
    
    return Command(update=update_dict)
```

**Gate Function** (`generation_review_gate`):

```python
def generation_review_gate(state: MainSupervisorState) -> Dict[str, Any]:
    """
    HITL gate for generation review (Artifacts & Workspace).
    """
    # Extract chart files
    helm_chart_artifacts = state.get("helm_chart_artifacts", {})
    chart_files = list(helm_chart_artifacts.keys())
    
    # Build review summary with workspace prompt
    full_summary = f"""# ✅ Template Generation Complete - Validation Required
    
    ## Generated Helm Chart Artifacts
    
    {chart_files_text}
    
    ## 📁 Workspace Directory Configuration
    
    **Please specify the workspace directory where the Helm chart should be written:**
    
    - Reply "approve" to use default: `/tmp/helm-charts`
    - Reply "approve /your/custom/path" to use custom directory
    """
    
    # Trigger interrupt
    human_decision = interrupt(review_data)
    
    # Extract workspace_dir from decision
    workspace_dir = human_decision.get("workspace_dir", "/tmp/helm-charts")
    
    # Update approval status
    updated_approvals = update_approval_status(...)
    
    return {
        "human_approval_status": updated_approvals,
        "workspace_dir": workspace_dir,  # Return workspace_dir
        "messages": [...]
    }
```

**Workspace Directory Extraction**:

The gate extracts `workspace_dir` from the human decision:
- **Default**: `/tmp/helm-charts` if user says "approve"
- **Custom**: Parses path from user response (e.g., "approve /home/user/charts")

### HITL Gate Rules

**From Supervisor Prompt**:

```
HITL APPROVAL GATES (REQUIRED):
- request_generation_review: Request human review of generated artifacts 
  and workspace selection (call after template_supervisor completes)

HITL GATE RULES:
- request_generation_review: Call IMMEDIATELY after template_supervisor completes. 
  Do NOT proceed to validation without approval. 
  If generation_approved is False, you MUST call this tool. You cannot skip it.
- Planning phase does NOT require approval - proceed directly to template generation
```

**Critical Rules**:

1. **Generation Gate is MANDATORY**: Must call `request_generation_review` after generation completes
2. **No Skipping**: Cannot proceed to validation without approval
3. **Planning Auto-Proceeds**: Planning phase does NOT require approval (proceeds directly to generation)
4. **Check Approval Status**: Always check `human_approval_status.generation.status` before validation

### Approval Status Tracking

**State Field**: `human_approval_status: Dict[str, ApprovalStatus]`

**Structure**:

```python
{
    "planning": ApprovalStatus(
        status="approved",  # "pending" | "approved" | "rejected" | "modified"
        reviewer="user@example.com",
        comments="LGTM",
        timestamp=datetime.now()
    ),
    "generation": ApprovalStatus(
        status="approved",
        reviewer="user@example.com",
        comments="approve /my/custom/path",
        timestamp=datetime.now()
    )
}
```

**Helper Functions**:

- `is_approved(state, phase)`: Checks if phase is approved
- `update_approval_status(state, approval_type, decision, reviewer, comments)`: Updates approval status

---

## Tool-Based Delegation

### Overview

The Supervisor Agent uses **tool wrappers** to delegate to swarms. Each swarm is wrapped in a `@tool` function that handles state transformation and invocation.

**Key Architectural Decision: Stateless Delegation**
Crucially, sub-swarms are invoked **statelessly** (using `ainvoke` without passing the parent's `thread_id`).
*   **Why**: Passing `thread_id` would couple the sub-swarm's state to the supervisor's checkpoint, causing conflicts when resuming diverse operations.
*   **Result**: The sub-swarm runs to completion in a single "turn" from the supervisor's perspective. If the sub-swarm needs HITL, it returns control to the supervisor, which handles the interrupt.
*   **Resumption**: The parent `A2AAutoPilotExecutor` manages the actual resumption of the workflow using `Command(resume=...)`.

### Swarm Tool Wrappers

#### 1. Planning Swarm Tool

**Tool**: `transfer_to_planning_swarm`

**Implementation**:

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
    - workflow_state.planning_complete == False
    - active_phase == "requirements"
    """
    # 1. Transform supervisor state → planning state
    planning_input = StateTransformer.supervisor_to_planning(runtime.state)
    
    # 2. Invoke planning swarm
    # NOTE: Invoked statelessly (no thread_id in config) to avoid checkpoint conflicts.
    planning_output = await planning_swarm.ainvoke(
        planning_input,
        config={"recursion_limit": 100}
    )
    
    # 3. Transform back
    supervisor_updates = StateTransformer.planning_to_supervisor(
        planning_output,
        runtime.state,
        tool_call_id
    )
    
    # 4. Return Command with state updates
    return Command(update=supervisor_updates)
```

**When to Call**:

- User requests Helm chart generation
- `workflow_state.planning_complete == False`
- `active_phase == "requirements"`

#### 2. Template Supervisor Tool

**Tool**: `transfer_to_template_supervisor`

**Implementation**:

```python
@tool
async def transfer_to_template_supervisor(
    task_description: str,
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Delegate to generation swarm for Helm chart code generation.
    
    Use this when:
    - Planning is complete (workflow_state.planning_complete == True)
    - Need to generate actual Helm chart files
    - active_phase == "planning"
    """
    # 1. Transform supervisor state → generation state
    generation_input = StateTransformer.supervisor_to_generation(runtime.state)
    
    # 2. Invoke generation swarm
    generation_output = await template_supervisor.ainvoke(
        generation_input,
        config={"recursion_limit": 100}
    )
    
    # 3. Transform back
    supervisor_updates = StateTransformer.generation_to_supervisor(
        generation_output,
        runtime.state,
        tool_call_id
    )
    
    # 4. Return Command with state updates
    return Command(update=supervisor_updates)
```

**When to Call**:

- `workflow_state.planning_complete == True`
- Planning approved (if required)
- `active_phase == "planning"`

#### 3. Validator Deep Agent Tool

**Tool**: `transfer_to_validator_deep_agent`

**Implementation**:

```python
@tool
async def transfer_to_validator_deep_agent(
    task_description: str,
    runtime: ToolRuntime[None, MainSupervisorState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    workspace_dir: Optional[str] = None
) -> Command:
    """
    Delegate to validation swarm for security and quality validation.
    
    Use this when:
    - Generation is complete (workflow_state.generation_complete == True)
    - Generation approved (workflow_state.generation_approved == True)
    - Need to validate generated Helm charts
    - active_phase == "generation"
    
    Args:
        workspace_dir: Optional. Explicitly specify the directory where the chart is located.
                       If not provided, defaults to the 'workspace_dir' in the state.
    """
    # Priority: Argument > State > Default
    final_workspace_dir = workspace_dir or runtime.state.get("workspace_dir", "/tmp/helm-charts")
    
    # 1. Transform supervisor state → validation state
    validation_input = StateTransformer.supervisor_to_validation(
        runtime.state, 
        workspace_dir=final_workspace_dir
    )
    
    # 2. Invoke validation swarm
    validation_output = await validator_deep_agent.ainvoke(
        validation_input,
        config={"recursion_limit": 100}
    )
    
    # 3. Transform back
    supervisor_updates = StateTransformer.validation_to_supervisor(
        validation_output,
        runtime.state,
        tool_call_id=tool_call_id
    )
    
    # 4. Update workspace_dir if provided
    if workspace_dir:
        supervisor_updates["workspace_dir"] = workspace_dir
    
    # 5. Return Command with state updates
    return Command(update=supervisor_updates)
```

**When to Call**:

- `workflow_state.generation_complete == True`
- `workflow_state.generation_approved == True` (MANDATORY)
- `active_phase == "generation"`

**Workspace Directory**:

- Can be provided as tool argument
- Falls back to `workspace_dir` in state (set during generation review)
- Defaults to `/tmp/helm-charts` if not specified

### Human Feedback Tool

**Tool**: `request_human_feedback`

**Purpose**: General-purpose tool for requesting human input during workflow

**Use Cases**:

- Clarifying ambiguous requirements
- Guiding users about capabilities (out-of-scope requests)
- Final workflow completion notification
- Marking deployment as complete

**Implementation**:

```python
@tool
def request_human_feedback(
    question: str,
    context: Optional[str] = None,
    phase: Optional[str] = None,
    mark_deployment_complete: bool = False,
    runtime: ToolRuntime[None, MainSupervisorState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = ""
) -> Command:
    """
    Request human feedback during workflow execution.
    
    This tool pauses execution and waits for human input.
    """
    # Build interrupt payload
    interrupt_payload = {
        "pending_feedback_requests": {
            "status": "input_required",
            "session_id": session_id,
            "question": question,
            "context": context or "No additional context provided",
            "active_phase": phase or "unknown"
        }
    }
    
    # Trigger interrupt - execution pauses here
    human_response = interrupt(interrupt_payload)
    
    # Handle deployment completion
    if mark_deployment_complete and runtime:
        # Guardrail: Verify validation is complete
        if not validation_complete:
            return Command(update={
                "messages": [ToolMessage(
                    content="SYSTEM ERROR: Cannot mark deployment complete - validation not complete",
                    tool_call_id=tool_call_id
                )]
            })
        
        # Update workflow state
        workflow_state_obj.set_phase_complete("deployment")
        workflow_state_obj.set_approval("deployment", True)
    
    # Return Command with human response
    return Command(update={
        "user_query": human_response_str,
        "messages": [tool_message],
        "llm_input_messages": [human_message],
        "workflow_state": workflow_state_obj  # If deployment marked complete
    })
```

**Final Workflow Step**:

After validation completes, the supervisor calls:

```python
request_human_feedback(
    question="Chart has been generated and validated. Please follow along the readme for deployment instructions.\n\nIf you found this helpful, please support us by starring our repository: https://github.com/talkops-ai/k8s-autopilot 🌟",
    mark_deployment_complete=True
)
```

This effectively completes the workflow (deployment is manual).

---

## Workflow Orchestration

### Workflow Sequence

The Supervisor Agent orchestrates the following workflow:

```
1. User Request
   ↓
2. transfer_to_planning_swarm()
   - Analyzes requirements
   - Creates chart plan
   - Sets workflow_state.planning_complete = True
   ↓
3. transfer_to_template_supervisor()
   - Generates Helm chart templates
   - Creates values.yaml, Chart.yaml, README.md
   - Sets workflow_state.generation_complete = True
   ↓
4. request_generation_review() [MANDATORY HITL GATE]
   - Shows generated artifacts
   - Requests workspace directory
   - Sets workflow_state.generation_approved = True
   - Sets workspace_dir in state
   ↓
5. transfer_to_validator_deep_agent()
   - Validates chart syntax (helm lint)
   - Validates template rendering (helm template)
   - Validates cluster compatibility (helm dry-run)
   - Sets workflow_state.validation_complete = True (on success)
   ↓
6. request_human_feedback(mark_deployment_complete=True)
   - Final notification
   - Marks workflow as complete
   ↓
7. Workflow Complete
```

### Workflow Rules

**From Supervisor Prompt**:

```
WORKFLOW SEQUENCE WITH HITL:
1. For ANY Helm chart request → transfer_to_planning_swarm(task_description="...")
2. When planning_complete → transfer_to_template_supervisor(task_description="...") 
   [Proceeds automatically]
3. When generation_complete (from template_supervisor) → STOP and call 
   request_generation_review() IMMEDIATELY. [REQUIRED BLOCKING STEP]
   - Do NOT proceed to validation.
   - Do NOT ask for feedback yet.
   - JUST call request_generation_review().
4. When generation_approved → transfer_to_validator_deep_agent(task_description="...")
5. When validation_complete → Call request_human_feedback with mark_deployment_complete=True

CRITICAL RULES:
- Check workflow_state flags before each tool call
- ALWAYS call HITL gate tools after phase completion (generation → request_generation_review)
- Do NOT proceed to next phase without approval (check human_approval_status)
- If approval status is "pending" or "rejected", wait or end workflow
- Always call tools immediately, don't describe what you will do
- Do NOT do any chart generation/validation yourself - ONLY delegate using tools
- Template generation proceeds automatically after planning completes (no approval needed)
- Validation proceeds automatically ONLY after generation is approved
- **NO AUTOMATED DEPLOYMENT**: Use request_human_feedback with mark_deployment_complete=True
```

### Stop Conditions

**When to Finish**:

- `workflow_state.workflow_complete == True` → Respond with completion summary and end
- Any phase is rejected AND user doesn't request changes → End workflow with error message
- **DO NOT** keep calling tools if workflow is already complete - check `workflow_state` first

### Loop Prevention

**Loop Counter**: `workflow_state.loop_counter` (max 30)

**Purpose**: Prevents infinite loops if agent keeps calling tools unnecessarily

**Increment**: Incremented on each phase transition

**Detection**: If `loop_counter > 30`, workflow stops with error

---

## Stream Processing

### Method: `stream()`

**Purpose**: Provides async streaming of workflow execution with interrupt detection

**Signature**:

```python
async def stream(
    self,
    query_or_command,
    context_id: str,
    task_id: str
) -> AsyncGenerator[AgentResponse, None]:
    """
    Simplified async stream method following the reference pattern.
    
    Features:
    - Simple state management using StateTransformer
    - Clean interrupt detection with '__interrupt__' key
    - Direct status-based response formatting
    - Minimal manual state handling
    """
```

### Stream Flow

#### 1. Initialization vs Resume

```python
if isinstance(query_or_command, Command):
    # Resume call: pass Command directly to graph
    # LangGraph will restore state from checkpoint using thread_id
    graph_input = query_or_command
    thread_id = context_id  # Reuse for HITL resume
else:
    # Initial call: create full state with Pydantic models
    user_query = str(query_or_command)
    graph_input = self._create_initial_state(
        user_query=user_query,
        context_id=context_id,
        task_id=task_id
    )
    thread_id = context_id  # Use context_id as thread_id
```

#### 2. Configuration

```python
config: RunnableConfig = {
    'configurable': {
        'thread_id': thread_id,
        'recursion_limit': getattr(self.config_instance, 'recursion_limit', 50)
    }
}

config_with_durability = {
    **config,
    "durability": "async",
    "subgraphs": True  # Required for proper subgraph handoff processing
}
```

#### 3. Streaming Loop

```python
async for item in self.compiled_graph.astream(
    graph_input, 
    config_with_durability, 
    stream_mode='values', 
    subgraphs=True
):
    step_count += 1
    
    # Unpack subgraph tuples if needed
    if isinstance(item, tuple) and len(item) == 2:
        namespace, state = item
        item = state
    
    # 1. Handle interrupts
    if '__interrupt__' in item:
        interrupt_response = self._handle_hitl_interrupt(...)
        if interrupt_response:
            yield interrupt_response
            break  # Pause streaming until resume
    
    # 2. Check workflow completion
    if is_workflow_complete:
        yield completion_response
        return  # CRITICAL: Use return, not break, to allow generator to close naturally
    
    # 3. Handle normal state updates
    yield processing_response
```

### Interrupt Detection

**Key**: `'__interrupt__'` in state item

**When Detected**:

1. Extract interrupt payload
2. Format as `AgentResponse` with `response_type='human_input'`
3. Yield interrupt response
4. **Break** streaming loop (pauses until resume) via `break` (for interrupts) or `return` (for completion).

**Resume**:

- Client calls `stream()` again with `Command(resume=human_response)`
- Graph execution resumes from exact interrupt point
- Streaming continues

---

## Interrupt Handling

### Method: `_handle_hitl_interrupt()`

**Purpose**: Handles various interrupt types and formats responses for client

**Interrupt Types**:

1. **Human Feedback Request** (`pending_feedback_requests`)
2. **Tool Result Review** (`tool_call_results_for_review`)
3. **Critical Tool Call Approval** (`pending_tool_calls`)
4. **Generic Interrupt** (unknown type)

**Implementation**:

```python
def _handle_hitl_interrupt(
    self,
    item: Dict[str, Any],
    context_id: str,
    task_id: str,
    step_count: int
) -> Optional[AgentResponse]:
    """
    Handle HITL interrupt and format response for client.
    """
    # Extract interrupt data
    interrupt_list = item.get('__interrupt__', [])
    interrupt_payload = interrupt_list[0].value if interrupt_list else {}
    
    # Detect interrupt type
    if "pending_feedback_requests" in interrupt_payload:
        # Handle feedback request interrupt
        feedback_data = interrupt_payload.get("pending_feedback_requests", {})
        content = {
            'type': 'human_feedback_request',
            'question': feedback_data.get('question', 'Input required'),
            'context': feedback_data.get('context', ''),
            'phase': feedback_data.get('active_phase', 'unknown'),
            'status': feedback_data.get('status', 'input_required'),
            'session_id': feedback_data.get('session_id', context_id)
        }
        
        return AgentResponse(
            response_type='human_input',
            is_task_complete=False,
            require_user_input=True,
            content=content,
            metadata={...}
        )
    
    # Handle other interrupt types...
```

### Interrupt Payload Structure

**Human Feedback Request**:

```python
{
    "pending_feedback_requests": {
        "status": "input_required",
        "session_id": "session-123",
        "question": "What workspace directory should I use?",
        "context": "No additional context provided",
        "active_phase": "generation"
    }
}
```

**Tool Result Review**:

```python
{
    "tool_call_results_for_review": {
        "tool_call_id": "call_123",
        "tool_name": "transfer_to_validator_deep_agent",
        "tool_args": {...},
        "tool_result": {...},
        "phase": "validation",
        "requires_review": True,
        "review_status": "pending"
    }
}
```

### Resume Flow

**Client Side**:

```python
# Initial call
async for response in supervisor.stream("Create Helm chart for nginx", context_id, task_id):
    if response.require_user_input:
        # Handle interrupt
        user_response = await get_user_input(response.content['question'])
        
        # Resume with user response
        async for response in supervisor.stream(
            Command(resume=user_response),
            context_id,
            task_id
        ):
            # Continue processing...
```

**Graph Side**:

- `interrupt()` returns the resume value
- Graph execution continues from interrupt point
- State is restored from checkpoint

---

## Integration with Swarms

### Swarm Registration

**During Initialization**:

```python
self.agents = {}
for agent in agents:
    # Set the checkpointer for each agent
    if hasattr(agent, 'memory'):
        agent.memory = self.memory  # Share checkpointer
    self.agents[agent.name] = agent
```

**Agent Names**:

- `planning_swarm_deep_agent`: Planning swarm
- `template_supervisor`: Template generation swarm
- `validator_deep_agent`: Validation swarm

### Graph Compilation

**During Graph Building**:

```python
compiled_swarms = {}
for agent_name, agent in self.agents.items():
    graph = agent.build_graph()
    
    # Check if graph is already compiled
    if hasattr(graph, 'compile'):
        compiled_graph = graph.compile(name=agent_name)
    else:
        compiled_graph = graph  # Already compiled (e.g., from create_deep_agent)
    
    compiled_swarms[agent_name] = compiled_graph
```

**Tool Creation**:

```python
# Create tool wrappers for each swarm
swarm_tools = self._create_swarm_tools(compiled_swarms)

# Tools created:
# - transfer_to_planning_swarm
# - transfer_to_template_supervisor
# - transfer_to_validator_deep_agent
```

### Shared Checkpointer

**Important**: All agents share the same checkpointer instance:

```python
# Supervisor checkpointer
self.memory = get_checkpointer(config=self.config_instance, prefer_postgres=True)

# Share with agents
for agent in agents:
    if hasattr(agent, 'memory'):
        agent.memory = self.memory
```

**Benefits**:

- **Unified State**: All state stored in same checkpoint store
- **Resumability**: Can resume from any phase
- **Consistency**: Single source of truth for workflow state

---

## Best Practices

### For Supervisor Behavior

1. **Always Check Workflow State**: Verify phase completion flags before tool calls
2. **Follow HITL Rules**: Call gate tools immediately after phase completion
3. **Don't Skip Gates**: Cannot proceed without approval
4. **Delegate, Don't Generate**: Use tools to delegate, don't generate charts yourself
5. **Check Completion**: Verify `workflow_state.workflow_complete` before continuing

### For State Transformation

1. **Use StateTransformer**: Always use `StateTransformer` methods for state conversion
2. **Pre-Write Files**: Pre-write chart files in `supervisor_to_validation()` to avoid context overload
3. **Create Summary Messages**: Use `ToolMessage` and `HumanMessage` for clear communication
4. **Update Workflow State**: Always update `workflow_state` phase completion flags

### For HITL Gates

1. **Check Approval First**: Always check `is_approved()` before triggering interrupt
2. **Extract Workspace**: Extract `workspace_dir` from generation review decision
3. **Update State Properly**: Merge approval status dictionaries correctly
4. **Provide Context**: Include clear questions and context in interrupt payloads

### For Tool Delegation

1. **Use Correct Tools**: Call the right tool for the current phase
2. **Pass Workspace Dir**: Pass `workspace_dir` to validator tool if user specified custom path
3. **Handle Errors**: Check tool results and handle errors appropriately
4. **Transform State**: Always use `StateTransformer` methods for state conversion

### For Stream Processing

1. **Handle Interrupts**: Always check for `'__interrupt__'` in stream items
2. **Break on Interrupt**: Break streaming loop when interrupt detected
3. **Resume Properly**: Use `Command(resume=...)` to resume execution
4. **Check Completion**: Verify workflow completion before continuing

---

## Summary

The Supervisor Agent is the central orchestrator for Helm chart generation workflows. It:

- ✅ **Orchestrates** workflow phases using tool-based delegation
- ✅ **Transforms** state between supervisor and swarm schemas
- ✅ **Manages** HITL gates for mandatory approvals
- ✅ **Streams** execution with interrupt detection
- ✅ **Tracks** workflow progress and prevents infinite loops
- ✅ **Integrates** seamlessly with planning, generation, and validation swarms

The agent uses LangChain's `create_agent()` pattern with tool wrappers, simplifying orchestration while maintaining flexibility and control over the workflow.
