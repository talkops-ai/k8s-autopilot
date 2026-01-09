# Generator Agent (Validator Deep Agent) - Comprehensive Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Implementation Details](#implementation-details)
4. [Prompts](#prompts)
5. [Tools](#tools)
6. [Self-Healing Mechanisms](#self-healing-mechanisms)
7. [Human-in-the-Loop (HITL)](#human-in-the-loop-hitl)
8. [State Management](#state-management)
9. [Integration with Supervisor](#integration-with-supervisor)
10. [Validation Workflow](#validation-workflow)
11. [Error Handling](#error-handling)
12. [Best Practices](#best-practices)

---

## Overview

The **Generator Agent** (also known as `k8sAutopilotValidatorDeepAgent`) is a Deep Agent responsible for validating, self-healing, and ensuring Helm charts are production-ready. It uses LangChain's DeepAgent framework with built-in file system tools and custom Helm validation tools to comprehensively validate Helm charts.

### Key Features

- ✅ **Deep Agent Pattern**: Uses ReAct (Reasoning → Action → Observation) pattern for autonomous problem-solving
- ✅ **Built-in File System Tools**: Automatic access to `ls`, `read_file`, `write_file`, `edit_file` via FilesystemBackend
- ✅ **Custom Helm Validators**: Three specialized validation tools (`helm_lint_validator`, `helm_template_validator`, `helm_dry_run_validator`)
- ✅ **Self-Healing**: Automatically fixes common errors using `edit_file` tool (indentation, deprecated APIs, missing fields)
- ✅ **Human-in-the-Loop**: Uses `ask_human` tool to pause execution and request human assistance when needed
- ✅ **Retry Logic**: Tracks retry counts per validator (max 2 retries) before escalating to human
- ✅ **State Persistence**: Uses LangGraph checkpointing for resumable execution
- ✅ **Workspace Management**: Manages chart files in isolated workspace directories

### Purpose

The Generator Agent receives generated Helm charts from the Template Coordinator Agent and:

1. **Validates** chart syntax, structure, and Kubernetes compatibility
2. **Fixes** auto-fixable issues (YAML indentation, deprecated APIs, missing fields)
3. **Escalates** complex issues to humans via `ask_human` tool
4. **Reports** validation results and blocking issues
5. **Prepares** charts for deployment readiness

---

## Architecture

### Deep Agent Pattern

The Generator Agent uses **DeepAgent** (`create_deep_agent` from `deepagents`), which provides:

- **ReAct Pattern**: Multi-step reasoning → action → observation cycles
- **Built-in Middleware**: File system tools, todo planning, context management
- **FilesystemBackend**: Real filesystem access for Helm commands
- **State Management**: Automatic state persistence via checkpointer

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Supervisor Agent                          │
│  (transfers via transfer_to_validator_deep_agent tool)      │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ State: generated_chart, chart_metadata
                       ▼
┌─────────────────────────────────────────────────────────────┐
│          Generator Agent (Validator Deep Agent)             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Deep Agent Core (create_deep_agent)                 │  │
│  │  - Model: LLM (GPT-4 or configured model)              │  │
│  │  - System Prompt: VALIDATOR_SUPERVISOR_PROMPT         │  │
│  │  - Checkpointer: MemorySaver (state persistence)      │  │
│  │  - Backend: FilesystemBackend (real filesystem)       │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Built-in Tools (Automatic)                           │  │
│  │  - ls: List files                                     │  │
│  │  - read_file: Read file contents                      │  │
│  │  - write_file: Write new files                        │  │
│  │  - edit_file: Edit existing files                     │  │
│  │  - write_todos: Plan validation tasks                 │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Custom Helm Validation Tools                         │  │
│  │  - helm_lint_validator: Syntax validation             │  │
│  │  - helm_template_validator: Template rendering        │  │
│  │  - helm_dry_run_validator: Cluster compatibility      │  │
│  │  - ask_human: Human-in-the-loop interrupts            │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Middleware                                            │  │
│  │  - ValidationStateMiddleware: Exposes state to tools  │  │
│  └──────────────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ Updates: validation_results, blocking_issues
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              State Updates Returned to Supervisor            │
│  - validation_results: List[ValidationResult]               │
│  - blocking_issues: List[str]                               │
│  - deployment_ready: bool                                   │
└─────────────────────────────────────────────────────────────┘
```

### Component Relationships

1. **Supervisor Agent** → Transfers state via `transfer_to_validator_deep_agent` tool
2. **State Transformer** → Converts `MainSupervisorState` → `ValidationSwarmState`
3. **Generator Agent** → Executes validation workflow using Deep Agent
4. **State Transformer** → Converts `ValidationSwarmState` → `MainSupervisorState` updates
5. **Supervisor Agent** → Receives validation results and continues workflow

---

## Implementation Details

### Class: `k8sAutopilotValidatorDeepAgent`

**Location**: `k8s_autopilot/core/agents/helm_generator/generator/generator_agent.py`

**Inheritance**: `BaseSubgraphAgent`

**Key Methods**:

```python
class k8sAutopilotValidatorDeepAgent(BaseSubgraphAgent):
    def __init__(
        self,
        config: Optional[Config] = None,
        custom_config: Optional[Dict[str, Any]] = None,
        name: str = "validator_deep_agent",
        memory: Optional[MemorySaver] = None,
        workspace_dir: str = "/tmp/helm-charts"
    ):
        """
        Initialize the validator deep agent.
        
        Args:
            config: Configuration object
            custom_config: Custom configuration dictionary
            name: Agent name for identification
            memory: LangGraph MemorySaver for checkpointing
            workspace_dir: Root directory for chart workspace
        """
```

**Initialization Steps**:

1. **Config Setup**: Uses centralized `Config` system for LLM configuration
2. **LLM Models**: Creates two LLM instances:
   - `self.model`: Standard LLM for general operations
   - `self.deep_agent_model`: LLM for Deep Agent (can be different model)
3. **State Initialization**: Creates `ValidationSwarmState()` instance
4. **Prompt Definition**: Sets `VALIDATOR_SUPERVISOR_PROMPT` as system prompt
5. **Workspace Setup**: Configures workspace directory for chart files

### Method: `build_graph()`

**Purpose**: Builds the Deep Agent graph using `create_deep_agent`

**Key Configuration**:

```python
self.validator_agent = create_deep_agent(
    model=self.deep_agent_model,
    system_prompt=self._validator_prompt,
    tools=[
        helm_lint_validator,
        helm_template_validator,
        helm_dry_run_validator,
        ask_human
        # Built-in tools (ls, read_file, write_file, edit_file) are automatically added
    ],
    checkpointer=self.memory,
    context_schema=ValidationSwarmState,
    middleware=[
        ValidationStateMiddleware(),  # Exposes state to tools
    ],
    backend=FilesystemBackend(root_dir=self.workspace_dir),  # Real filesystem access
)
```

**Important Points**:

- **FilesystemBackend**: Required for real filesystem access (Helm commands need actual file paths)
- **Built-in Tools**: Automatically available via DeepAgent middleware
- **Custom Tools**: Explicitly provided in `tools` list
- **Middleware**: `ValidationStateMiddleware` exposes `ValidationSwarmState` to tools via `runtime.state`
- **Checkpointer**: Enables state persistence and resumable execution

### Middleware: `ValidationStateMiddleware`

**Purpose**: Exposes `ValidationSwarmState` to tools via `runtime.state`

**Implementation**:

```python
class ValidationStateMiddleware(AgentMiddleware):
    """
    Middleware to expose ValidationSwarmState to tools.
    
    This ensures all state fields (generated_chart, chart_metadata, 
    validation_results, etc.) are available in runtime.state for tools.
    """
    state_schema = ValidationSwarmState
    tools = [
        helm_lint_validator,
        helm_template_validator,
        helm_dry_run_validator
    ]
```

**Why Needed**: DeepAgent tools need access to state fields (e.g., `generated_chart`, `chart_metadata`) to perform validation. This middleware ensures state is accessible via `runtime.state` in tool implementations.

---

## Prompts

### System Prompt: `VALIDATOR_SUPERVISOR_PROMPT`

**Location**: `k8s_autopilot/core/agents/helm_generator/generator/generator_prompts.py`

**Purpose**: Provides comprehensive instructions to the Deep Agent on how to validate Helm charts, fix errors, and escalate to humans.

**Key Sections**:

#### 1. Mission Statement

```
You are an expert Helm chart validation specialist responsible for ensuring Helm charts
meet quality, security, and best practice standards before deployment.
```

#### 2. Available Tools

- **Built-in File System Tools**: `ls`, `read_file`, `write_file`, `edit_file`
- **Custom Helm Validators**: `helm_lint_validator`, `helm_template_validator`, `helm_dry_run_validator`
- **Human-in-the-Loop**: `ask_human`

#### 3. Validation Workflow

1. **Prepare Chart Files**: Write `generated_chart` dictionary to workspace filesystem
2. **Inspect Chart Structure**: Use `ls` and `read_file` to verify structure
3. **Run Validations**: Execute validators in order (lint → template → dry-run)
4. **Handle Results**: Fix errors, retry (max 2), escalate to human if needed

#### 4. Self-Healing Instructions

```
If errors found:
  - Read problematic files with read_file
  - Use edit_file to fix auto-fixable issues (indentation, deprecated APIs)
  - Re-run validation
```

#### 5. Human Escalation Rules

**MANDATORY `ask_human` scenarios**:

1. **After 2 failed retry attempts** - Stop retrying and ask user
2. **Missing critical information** - Version numbers, domain names, etc.
3. **Ambiguous errors** - Unclear error messages
4. **Trade-off decisions** - Multiple valid approaches
5. **Cluster-specific issues** - RBAC, admission controllers, etc.

#### 6. Retry Logic

```
- MAX 2 retry attempts per validation
- Check validation_retry_counts before retrying
- If validation_retry_counts.get(validator_name, 0) >= 2, call ask_human
```

#### 7. State Management

- `validation_results`: List of ValidationResult objects (use `add` reducer)
- `blocking_issues`: List of strings (use `add` reducer)
- `validation_retry_counts`: Dict mapping validator name to retry count
- `deployment_ready`: Boolean indicating readiness

---

## Tools

### Built-in Tools (Automatic)

These tools are automatically available via DeepAgent's FilesystemMiddleware:

#### 1. `ls` - List Files

**Usage**: `ls /workspace/{chart_name}/templates/`

**Purpose**: Inspect chart directory structure

**Example**:
```
ls /workspace/my-app/
# Returns: Chart.yaml, values.yaml, templates/, .helmignore
```

#### 2. `read_file` - Read File Contents

**Usage**: `read_file /workspace/{chart_name}/values.yaml`

**Purpose**: Read file contents for inspection or analysis

**Features**:
- Supports line ranges: `read_file /path/to/file lines=10-20`
- Used before attempting fixes

**Example**:
```
read_file /workspace/my-app/templates/deployment.yaml
# Returns: Full file content
```

#### 3. `write_file` - Write New Files

**Usage**: `write_file /workspace/{chart_name}/Chart.yaml` with content

**Purpose**: Write chart files from `generated_chart` dictionary to filesystem

**Workflow Step**:
```
When receiving generated_chart in state:
1. Extract chart name from chart_metadata.chart_name
2. Create workspace directory: /workspace/{chart_name}/
3. Write all chart files using write_file:
   - write_file /workspace/{chart_name}/Chart.yaml
   - write_file /workspace/{chart_name}/values.yaml
   - write_file /workspace/{chart_name}/templates/{template_name}.yaml
```

#### 4. `edit_file` - Edit Existing Files

**Usage**: `edit_file /workspace/{chart_name}/templates/deployment.yaml` with instructions

**Purpose**: Fix auto-fixable issues autonomously

**Common Fixes**:
- **YAML Indentation**: Fix indentation errors
- **Deprecated APIs**: Update to current API versions
- **Missing Fields**: Add required fields with sensible defaults

**Example**:
```
edit_file /workspace/my-app/templates/deployment.yaml
Instructions: "Fix YAML indentation error at line 45. Ensure proper spacing."
```

#### 5. `write_todos` - Plan Validation Tasks

**Usage**: `write_todos` with task list

**Purpose**: Create validation plan for task decomposition

**Example**:
```
write_todos([
    "Write chart files to workspace",
    "Run helm_lint_validator",
    "Run helm_template_validator",
    "Run helm_dry_run_validator",
    "Fix any errors found",
    "Report validation results"
])
```

### Custom Helm Validation Tools

#### 1. `helm_lint_validator`

**Location**: `k8s_autopilot/core/agents/helm_generator/generator/tools/helm_validator_tools.py`

**Purpose**: Fast syntax and structure validation using `helm lint`

**Signature**:
```python
@tool
def helm_lint_validator(
    chart_path: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
```

**Implementation Details**:

1. **Retry Count Check**: Reads `validation_retry_counts` from state
2. **Helm Command**: Executes `helm lint {chart_path}`
3. **Error Parsing**: Counts ERROR and WARNING occurrences
4. **Status Determination**: 
   - `passed = result.returncode == 0`
   - `severity = "error" if errors_count > 0 else "warning" if warnings_count > 0 else "info"`
5. **State Updates**:
   - Creates `ValidationResult` object
   - Updates `validation_results` list
   - Increments `validation_retry_counts` on failure
   - Resets retry count on success
   - Adds to `blocking_issues` if failed
6. **Max Retries Warning**: If `retry_count >= 2`, adds warning message to prompt `ask_human`

**Return Value**: `Command` with state updates

**Example Output**:
```python
ValidationResult(
    validator="helm_lint",
    passed=True,
    severity="info",
    message="Helm lint validation passed. 2 warning(s) found.",
    details={
        "exit_code": 0,
        "stdout": "...",
        "stderr": "...",
        "errors_count": 0,
        "warnings_count": 2,
        "retry_count": 0
    }
)
```

#### 2. `helm_template_validator`

**Purpose**: Template rendering and YAML validation using `helm template`

**Signature**:
```python
@tool
def helm_template_validator(
    chart_path: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    values_file: Optional[str] = None,
) -> Command:
```

**Implementation Details**:

1. **Helm Command**: Executes `helm template release {chart_path} [-f {values_file}]`
2. **YAML Validation**: Parses rendered output with `yaml.safe_load_all()`
3. **Status Determination**:
   - `passed = result.returncode == 0 and yaml_valid`
   - Checks for YAML syntax errors
4. **Error Handling**: Distinguishes between Helm errors and YAML parsing errors
5. **State Updates**: Similar to `helm_lint_validator`

**Use Case**: Validates that templates render to valid YAML before cluster validation

#### 3. `helm_dry_run_validator`

**Purpose**: Cluster compatibility validation using `helm install --dry-run`

**Signature**:
```python
@tool
def helm_dry_run_validator(
    chart_path: str,
    release_name: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    namespace: str = "default",
) -> Command:
```

**Implementation Details**:

1. **Helm Command**: Executes `helm install {release_name} {chart_path} --namespace {namespace} --dry-run --debug`
2. **Cluster Requirement**: Requires kubectl configured and active cluster connection
3. **Validation**: Checks API compatibility, resource constraints, RBAC, admission controllers
4. **Timeout**: 120 seconds (longer than lint/template due to cluster communication)
5. **Error Patterns**: Detects common cluster-specific errors (RBAC, resource quotas, etc.)

**Use Case**: Final validation before deployment to ensure chart works in target cluster

#### 4. `ask_human`

**Purpose**: Human-in-the-loop interrupt to request assistance

**Signature**:
```python
@tool
def ask_human(
    question: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
```

**Implementation Details**:

1. **Interrupt Creation**: Prepares interrupt payload with question and context
2. **LangGraph Interrupt**: Calls `interrupt(interrupt_payload)` to pause execution
3. **Execution Pause**: Graph execution pauses until user responds
4. **Resume**: After user response, execution resumes from exact point
5. **State Update**: Creates `ToolMessage` with user response

**Interrupt Payload Structure**:
```python
{
    "pending_feedback_requests": {
        "status": "input_required",
        "question": question,
        "context": "Validation agent needs human assistance",
        "active_phase": "validation",
        "tool_call_id": tool_call_id
    }
}
```

**Critical Note**: The `interrupt()` function raises a `GraphInterrupt` exception that MUST propagate to the graph executor. Do NOT wrap in try-except that catches the interrupt.

**When to Use**: See [Human-in-the-Loop](#human-in-the-loop-hitl) section

---

## Self-Healing Mechanisms

### Overview

The Generator Agent implements **autonomous error recovery** using the Deep Agent's ReAct pattern and built-in `edit_file` tool. It can fix common validation errors without human intervention.

### Self-Healing Workflow

```
Validation Error Detected
    ↓
Agent analyzes error (via LLM reasoning)
    ↓
Determines if auto-fixable
    ├─ Yes (confidence ≥ 0.75) → Attempt fix
    └─ No (confidence < 0.75) → Escalate to human
    ↓
[If auto-fixable]
    ↓
Read problematic file: read_file /workspace/{chart}/templates/deployment.yaml
    ↓
Fix using edit_file with specific instructions
    ↓
Re-run validation: helm_lint_validator(...)
    ↓
Check result
    ├─ Pass → Continue to next validation
    └─ Fail → Increment retry_count
        ├─ retry_count < 2 → Retry fix
        └─ retry_count >= 2 → Call ask_human
```

### Auto-Fixable Error Categories

#### 1. YAML Indentation Errors

**Detection**: Error message contains "indentation", "bad indentation", or YAML parsing errors

**Fix Strategy**:
```
edit_file /workspace/{chart}/templates/deployment.yaml
Instructions: "Fix YAML indentation error at line {line_number}. Ensure proper spacing (2 spaces per level)."
```

**Confidence**: 0.95 (high - straightforward fix)

**Example**:
```
Error: "templates/deployment.yaml: line 45: bad indentation"
Fix: Agent reads file, identifies line 45, fixes indentation, writes back
```

#### 2. Deprecated API Versions

**Detection**: Error message contains "deprecated", "no longer available", or API version warnings

**Fix Strategy**:
```
Common migrations:
- extensions/v1beta1 → apps/v1 (Deployment)
- apps/v1beta1 → apps/v1 (Deployment)
- batch/v1beta1 → batch/v1 (CronJob)
- apiextensions.k8s.io/v1beta1 → apiextensions.k8s.io/v1 (CRD)
```

**Fix Process**:
1. Read template file
2. Search for deprecated API version
3. Replace with current version
4. Update any required field changes (e.g., `spec.selector` for Deployment)

**Confidence**: 0.90 (high - well-documented migrations)

**Example**:
```
Error: "apiVersion 'extensions/v1beta1' is deprecated"
Fix: 
  read_file → Find "apiVersion: extensions/v1beta1"
  edit_file → Replace with "apiVersion: apps/v1"
  Update spec.selector.matchLabels if needed
```

#### 3. Missing Required Fields

**Detection**: Error message contains "required", "missing", or validation errors about missing fields

**Fix Strategy**:
```
1. Identify missing field from error message
2. Determine sensible default value
3. Add field to appropriate location in YAML
```

**Confidence**: 0.85 (medium-high - requires understanding of Kubernetes schemas)

**Example**:
```
Error: "Chart.yaml: version is required"
Fix:
  read_file Chart.yaml
  edit_file Chart.yaml
  Instructions: "Add version field with value '1.0.0'"
```

#### 4. Invalid Values

**Detection**: Error message indicates invalid value for a field

**Fix Strategy**:
```
1. Identify invalid value
2. Suggest valid options from error message
3. Update to valid value
```

**Confidence**: 0.70 (medium - may require domain knowledge)

**Example**:
```
Error: "service.type must be one of: ClusterIP, NodePort, LoadBalancer"
Fix:
  read_file values.yaml
  edit_file values.yaml
  Instructions: "Change service.type from 'ClusterIPs' to 'ClusterIP'"
```

### Retry Logic

**Maximum Retries**: 2 attempts per validator

**Retry Count Tracking**:
```python
validation_retry_counts: Dict[str, int] = {
    "helm_lint": 1,
    "helm_template": 0,
    "helm_dry_run": 2
}
```

**Retry Flow**:
1. **First Failure**: `retry_count = 0` → Attempt fix → `retry_count = 1`
2. **Second Failure**: `retry_count = 1` → Attempt fix → `retry_count = 2`
3. **Third Failure**: `retry_count = 2` → **STOP** → Call `ask_human`

**Reset on Success**: When validation passes, retry count resets to 0

**Implementation in Tools**:
```python
# Get current retry count
current_retry_counts = runtime.state.get("validation_retry_counts", {}) or {}
current_retry_count = current_retry_counts.get("helm_lint", 0)

# On failure: increment
if not passed:
    new_retry_count = current_retry_count + 1
    updated_retry_counts = {**current_retry_counts, "helm_lint": new_retry_count}
    update_dict["validation_retry_counts"] = updated_retry_counts
    
    # Add warning if max retries reached
    if new_retry_count >= 2:
        message += " ⚠️ MAX RETRIES REACHED - Call ask_human for assistance."

# On success: reset
else:
    updated_retry_counts = {**current_retry_counts, "helm_lint": 0}
    update_dict["validation_retry_counts"] = updated_retry_counts
```

### Self-Healing Examples

#### Example 1: YAML Indentation Fix

**Initial Error**:
```
helm_lint_validator → FAILED
Error: "templates/deployment.yaml: line 23: bad indentation"
```

**Agent Actions**:
1. `read_file /workspace/my-app/templates/deployment.yaml lines=20-25`
2. Identifies indentation issue at line 23
3. `edit_file /workspace/my-app/templates/deployment.yaml`
   - Instructions: "Fix indentation at line 23. Ensure 'containers:' is indented with 6 spaces (3 levels)"
4. `helm_lint_validator(chart_path="/workspace/my-app")` → **PASSED**

**Result**: Error fixed autonomously, validation continues

#### Example 2: Deprecated API Fix

**Initial Error**:
```
helm_lint_validator → FAILED
Warning: "apiVersion 'extensions/v1beta1' is deprecated, use 'apps/v1' instead"
```

**Agent Actions**:
1. `read_file /workspace/my-app/templates/deployment.yaml`
2. Finds `apiVersion: extensions/v1beta1`
3. `edit_file /workspace/my-app/templates/deployment.yaml`
   - Instructions: "Update apiVersion from 'extensions/v1beta1' to 'apps/v1'. Ensure spec.selector.matchLabels exists."
4. `helm_lint_validator(chart_path="/workspace/my-app")` → **PASSED**

**Result**: Deprecated API updated, validation continues

#### Example 3: Max Retries Reached

**Initial Error**:
```
helm_template_validator → FAILED
Error: "YAML parse error: unexpected character at line 50"
```

**Agent Actions**:
1. **Attempt 1**: `read_file` → `edit_file` → `helm_template_validator` → **FAILED** (retry_count = 1)
2. **Attempt 2**: `read_file` → `edit_file` → `helm_template_validator` → **FAILED** (retry_count = 2)
3. **Attempt 3**: Agent checks `retry_count >= 2` → **STOP** → `ask_human(...)`

**Result**: Escalated to human after 2 failed attempts

---

## Human-in-the-Loop (HITL)

### Overview

The Generator Agent uses the `ask_human` tool to pause execution and request human assistance when it cannot autonomously resolve validation issues.

### When to Call `ask_human` (MANDATORY)

The prompt explicitly defines **5 mandatory scenarios**:

#### 1. After 2 Failed Retry Attempts

**Trigger**: `validation_retry_counts.get(validator_name, 0) >= 2`

**Example**:
```python
ask_human(
    question="Helm lint validation failed after 2 fix attempts. Error: 'Chart.yaml: version is required'. I attempted to add a default version, but validation still fails. What version should I use for this chart?"
)
```

**Why**: Prevents infinite retry loops and ensures human oversight for persistent issues

#### 2. Missing Critical Information

**Trigger**: Need configuration values that cannot be inferred

**Examples**:
- Version numbers (cannot auto-generate meaningful versions)
- Domain names (require user input)
- API keys or secrets (security-sensitive)
- Resource quotas (cluster-specific)

**Example**:
```python
ask_human(
    question="The chart is missing a version number in Chart.yaml. What version should I use? (e.g., 1.0.0, 0.1.0)"
)
```

#### 3. Ambiguous Errors

**Trigger**: Error message is unclear and fix cannot be determined

**Example**:
```python
ask_human(
    question="Helm template validation failed with error: 'Error: template: my-app/templates/deployment.yaml:45: function \"unknownFunction\" not defined'. I cannot determine what function should be used here. Please provide guidance on how to resolve this issue."
)
```

#### 4. Trade-off Decisions

**Trigger**: Multiple valid approaches exist, need user preference

**Example**:
```python
ask_human(
    question="Helm dry-run failed with RBAC error. The chart requires cluster-admin permissions for a ClusterRole. Should I: (a) reduce permissions to a more restrictive Role, (b) document the requirement in the README, or (c) skip dry-run validation for this chart?"
)
```

#### 5. Cluster-Specific Issues

**Trigger**: Dry-run fails due to cluster configuration

**Examples**:
- RBAC restrictions
- Admission controller policies
- Resource quotas
- Network policies
- Storage class availability

**Example**:
```python
ask_human(
    question="Helm dry-run failed with error: 'persistentvolumeclaims \"data\" is forbidden: exceeded quota'. The chart requires 20Gi storage but cluster quota allows only 10Gi. Should I: (a) reduce storage size to 10Gi, (b) skip dry-run validation, or (c) document the requirement?"
)
```

### `ask_human` Implementation

**Tool Signature**:
```python
@tool
def ask_human(
    question: str,
    runtime: ToolRuntime[None, ValidationSwarmState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
```

**Execution Flow**:

1. **Prepare Interrupt Payload**:
```python
interrupt_payload = {
    "pending_feedback_requests": {
        "status": "input_required",
        "question": question,
        "context": "Validation agent needs human assistance",
        "active_phase": "validation",
        "tool_call_id": tool_call_id
    }
}
```

2. **Trigger Interrupt**:
```python
user_feedback = interrupt(interrupt_payload)
```

3. **Execution Pauses**: Graph execution pauses at this point
   - State is checkpointed
   - User receives question via interrupt handler
   - User provides response

4. **Resume Execution**: After user response, execution resumes
   - `user_feedback` contains user's response
   - Agent continues with user's input

5. **State Update**:
```python
tool_message = ToolMessage(
    content=f"User response: {user_feedback}",
    tool_call_id=tool_call_id
)

return Command(
    update={
        "messages": [tool_message]
    }
)
```

### Interrupt Handling in Supervisor

The interrupt propagates to the supervisor agent, which handles it via LangGraph's interrupt mechanism:

```python
# In supervisor agent execution
result = await graph.ainvoke(state, config=config)

if "__interrupt__" in result:
    # Handle interrupt
    interrupt_data = result["__interrupt__"]
    question = interrupt_data["pending_feedback_requests"]["question"]
    
    # Present to user (via API, CLI, etc.)
    user_response = await get_user_input(question)
    
    # Resume with user response
    final_result = await graph.ainvoke(
        Command(resume=user_response),
        config=config
    )
```

### HITL Best Practices

1. **Be Specific**: Questions should be clear and actionable
2. **Provide Context**: Include error details and attempted fixes
3. **Offer Options**: When possible, provide multiple choice options
4. **Don't Retry After HITL**: Once `ask_human` is called, don't retry the same fix
5. **Document Decisions**: Update `blocking_issues` with human-provided solutions

---

## State Management

### State Schema: `ValidationSwarmState`

**Location**: `k8s_autopilot/core/state/base.py`

**Definition**:
```python
class ValidationSwarmState(AgentState):
    """State for Validation & Deployment Swarm"""
    
    # Message history
    messages: Annotated[List[AnyMessage], add_messages]
    
    # Deep Agent required fields
    remaining_steps: Annotated[Optional[int], lambda x, y: y]
    
    # Active agent tracking
    active_agent: Annotated[Optional[Literal[
        "chart_validator",
        "security_scanner",
        "test_generator",
        "argocd_configurator",
        "validation_supervisor"
    ]], lambda x, y: y]
    
    # Inputs from generation swarm
    generated_chart: NotRequired[Dict[str, str]]  # filename -> content
    chart_metadata: NotRequired[ChartPlan]
    
    # Validation results (accumulative)
    validation_results: NotRequired[Annotated[List[ValidationResult], add]]
    security_scan_results: NotRequired[Optional[SecurityScanReport]]
    test_artifacts: NotRequired[Optional[Dict[str, str]]]
    argocd_manifests: NotRequired[Optional[ArgoCDConfig]]
    
    # Deployment readiness
    deployment_ready: NotRequired[bool]
    blocking_issues: NotRequired[Annotated[List[str], add]]
    
    # Retry tracking
    validation_retry_counts: NotRequired[Annotated[
        Dict[str, int], 
        lambda x, y: {**(x or {}), **(y or {})}
    ]]
    
    # Session tracking
    session_id: NotRequired[str]
    task_id: NotRequired[str]
    
    # Deep Agent features
    todos: NotRequired[Annotated[List[Dict[str, Any]], add]]
    
    # Handoff context
    handoff_metadata: NotRequired[Dict]
```

### State Reducers

#### 1. `add_messages` (Messages)

**Purpose**: Accumulates messages in conversation history

**Usage**: All tool messages are added to `messages` list

**Example**:
```python
update_dict = {
    "messages": [tool_message]  # Added to existing messages
}
```

#### 2. `add` (Validation Results, Blocking Issues)

**Purpose**: Accumulates validation results and blocking issues

**Usage**: 
```python
validation_results: Annotated[List[ValidationResult], add]
blocking_issues: Annotated[List[str], add]
```

**Behavior**: New items are appended to existing list (never overwritten)

**Example**:
```python
# Initial state
validation_results = [result1]

# Tool adds result2
update_dict = {
    "validation_results": [result2]  # Becomes [result1, result2]
}
```

#### 3. Merge Reducer (Retry Counts)

**Purpose**: Merges retry count dictionaries

**Usage**:
```python
validation_retry_counts: Annotated[
    Dict[str, int], 
    lambda x, y: {**(x or {}), **(y or {})}
]
```

**Behavior**: Merges dictionaries, with new values overriding old ones

**Example**:
```python
# Initial state
validation_retry_counts = {"helm_lint": 1}

# Tool updates
update_dict = {
    "validation_retry_counts": {"helm_lint": 2, "helm_template": 1}
}

# Result: {"helm_lint": 2, "helm_template": 1}
```

### State Flow

#### Input State (from Supervisor)

```python
{
    "messages": [HumanMessage(content="Validate this chart")],
    "generated_chart": {
        "Chart.yaml": "...",
        "values.yaml": "...",
        "templates/deployment.yaml": "..."
    },
    "chart_metadata": ChartPlan(...),
    "validation_results": [],
    "blocking_issues": [],
    "validation_retry_counts": {}
}
```

#### During Validation

State accumulates:
- `validation_results`: One per validator execution
- `blocking_issues`: One per validation failure
- `validation_retry_counts`: Updated per validator retry

#### Output State (to Supervisor)

```python
{
    "messages": [...],  # All tool messages + final summary
    "validation_results": [
        ValidationResult(validator="helm_lint", passed=True, ...),
        ValidationResult(validator="helm_template", passed=True, ...),
        ValidationResult(validator="helm_dry_run", passed=True, ...)
    ],
    "blocking_issues": [],  # Empty if all passed
    "deployment_ready": True,
    "validation_retry_counts": {"helm_lint": 0, "helm_template": 0, "helm_dry_run": 0}
}
```

---

## Integration with Supervisor

### State Transformation

The Generator Agent receives state from the Supervisor Agent via `StateTransformer.supervisor_to_validation()`.

#### Supervisor → Validation Transformation

**Location**: `k8s_autopilot/core/state/state_transformer.py`

**Transformation Logic**:
```python
@staticmethod
def supervisor_to_validation(
    supervisor_state: MainSupervisorState, 
    workspace_dir: str = "/tmp/helm-charts"
) -> Dict:
    """
    Transform supervisor state to validation swarm state.
    
    Maps:
    - helm_chart_artifacts → generated_chart (Dict[str, str])
    - planner_output → chart_metadata (ChartPlan)
    - Creates chart_path from workspace_dir and chart_name
    """
    # Extract chart artifacts
    generated_chart = supervisor_state.get("helm_chart_artifacts", {})
    
    # Extract chart metadata
    planner_output = supervisor_state.get("planner_output", {})
    chart_name = planner_output.get("chart_name", "unknown-chart")
    
    # Create chart path
    chart_path = f"{workspace_dir}/{chart_name}"
    
    # Create instruction message
    instruction = f"Validate the Helm chart located at {chart_path}"
    
    return {
        "messages": [HumanMessage(content=instruction)],
        "active_agent": "validation_supervisor",
        "generated_chart": generated_chart,
        "chart_metadata": ChartPlan(**planner_output),
        "validation_results": [],
        "blocking_issues": [],
        "handoff_metadata": {
            "chart_name": chart_name,
            "chart_path": chart_path,
            "workspace_dir": workspace_dir
        }
    }
```

#### Validation → Supervisor Transformation

**Transformation Logic**:
```python
@staticmethod
def validation_to_supervisor(
    validation_state: ValidationSwarmState,
    original_supervisor_state: MainSupervisorState,
    tool_call_id: str = "validation_complete"
) -> Dict:
    """
    Transform validation swarm output back to supervisor state updates.
    
    Maps:
    - validation_results → validation_results (List[ValidationResult])
    - blocking_issues → blocking_issues (List[str])
    - Updates workflow_state.validation_complete
    """
    # Extract validation results
    validation_results = validation_state.get("validation_results", [])
    
    # Extract blocking issues
    blocking_issues = validation_state.get("blocking_issues", [])
    
    # Update workflow state
    workflow_state = original_supervisor_state.get("workflow_state")
    if workflow_state:
        workflow_state.set_phase_complete("validation")
    
    # Create summary message
    summary = f"Validation complete. {len(validation_results)} validations run."
    if blocking_issues:
        summary += f" {len(blocking_issues)} blocking issues found."
    
    return {
        "validation_results": validation_results,
        "blocking_issues": blocking_issues,
        "workflow_state": workflow_state,
        "messages": [ToolMessage(content=summary, tool_call_id=tool_call_id)]
    }
```

### Supervisor Tool: `transfer_to_validator_deep_agent`

**Location**: `k8s_autopilot/core/agents/supervisor_agent.py`

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
    Transfer to validator deep agent for chart validation.
    
    Args:
        task_description: Description of validation task
        runtime: Supervisor runtime
        tool_call_id: Tool call ID
        workspace_dir: Optional workspace directory override
    """
    # 1. Transform supervisor state → validation state
    validation_input = StateTransformer.supervisor_to_validation(
        runtime.state, 
        workspace_dir=workspace_dir or runtime.state.get("workspace_dir", "/tmp/helm-charts")
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
    
    # 4. Return Command with state updates
    return Command(update=supervisor_updates)
```

### Integration Flow

```
Supervisor Agent
    │
    │ transfer_to_validator_deep_agent(task_description="...")
    │
    ▼
StateTransformer.supervisor_to_validation()
    │
    │ Creates ValidationSwarmState with:
    │ - generated_chart (from helm_chart_artifacts)
    │ - chart_metadata (from planner_output)
    │ - chart_path (workspace_dir/chart_name)
    │
    ▼
Generator Agent (Validator Deep Agent)
    │
    │ Executes validation workflow:
    │ 1. Write chart files to workspace
    │ 2. Run validations (lint → template → dry-run)
    │ 3. Fix errors autonomously
    │ 4. Escalate to human if needed
    │
    ▼
StateTransformer.validation_to_supervisor()
    │
    │ Updates MainSupervisorState with:
    │ - validation_results
    │ - blocking_issues
    │ - workflow_state.validation_complete = True
    │
    ▼
Supervisor Agent
    │
    │ Receives validation results
    │ Continues workflow (e.g., final review)
```

---

## Validation Workflow

### Complete Workflow Steps

#### Step 1: Receive Chart from Supervisor

**Input**: `ValidationSwarmState` with `generated_chart` dictionary

**State Fields**:
- `generated_chart`: `Dict[str, str]` mapping filename → content
- `chart_metadata`: `ChartPlan` with chart name, version, etc.
- `handoff_metadata`: Contains `chart_path` and `workspace_dir`

#### Step 2: Prepare Chart Files

**Agent Actions**:
1. Extract chart name: `chart_name = chart_metadata.chart_name`
2. Create workspace directory: `/workspace/{chart_name}/`
3. Write all chart files using `write_file`:
   ```
   write_file /workspace/{chart_name}/Chart.yaml
   write_file /workspace/{chart_name}/values.yaml
   write_file /workspace/{chart_name}/templates/deployment.yaml
   write_file /workspace/{chart_name}/templates/service.yaml
   ... (all files from generated_chart dictionary)
   ```

**Deep Agent Planning**: Agent may use `write_todos` to plan this step

#### Step 3: Inspect Chart Structure

**Agent Actions**:
1. `ls /workspace/{chart_name}/` - Verify all files exist
2. `ls /workspace/{chart_name}/templates/` - List templates
3. `read_file /workspace/{chart_name}/Chart.yaml` - Verify metadata
4. `read_file /workspace/{chart_name}/values.yaml` - Review configuration

**Purpose**: Ensure chart structure is correct before validation

#### Step 4: Run Validations (Sequential)

**Order**: lint → template → dry-run

##### 4.1 Helm Lint Validation

**Tool**: `helm_lint_validator(chart_path="/workspace/{chart_name}")`

**Purpose**: Fast syntax and structure validation

**On Failure**:
1. Read problematic files: `read_file /workspace/{chart_name}/templates/{file}.yaml`
2. Fix using `edit_file`: Fix indentation, deprecated APIs, missing fields
3. Re-run validation: `helm_lint_validator(...)`
4. Check retry count: If `retry_count >= 2`, call `ask_human`

**On Success**: Continue to template validation

##### 4.2 Template Validation

**Tool**: `helm_template_validator(chart_path="/workspace/{chart_name}")`

**Purpose**: Validate templates render to valid YAML

**On Failure**:
1. Read template file with YAML error
2. Fix YAML syntax using `edit_file`
3. Re-run validation
4. Check retry count: If `retry_count >= 2`, call `ask_human`

**On Success**: Continue to dry-run validation

##### 4.3 Dry-Run Validation

**Tool**: `helm_dry_run_validator(
    chart_path="/workspace/{chart_name}",
    release_name="{chart_name}",
    namespace="default"
)`

**Purpose**: Validate cluster compatibility

**On Failure**:
1. Analyze error (RBAC, quotas, API compatibility)
2. If fixable: Fix and retry
3. If cluster-specific: Call `ask_human` (no retry needed for cluster issues)

**On Success**: All validations passed

#### Step 5: Handle Validation Results

**If All Validations Pass**:
- Set `deployment_ready: true`
- Update `validation_results` with all outcomes
- Create summary message

**If Validation Fails**:
- Add to `blocking_issues`
- Set `deployment_ready: false`
- Document errors in `validation_results`
- If max retries reached: Call `ask_human`

#### Step 6: Return Results to Supervisor

**Output**: Updated `ValidationSwarmState` with:
- `validation_results`: List of all validation outcomes
- `blocking_issues`: List of blocking problems (if any)
- `deployment_ready`: Boolean indicating readiness
- `messages`: Summary message

---

## Error Handling

### Error Categories

#### 1. Helm Command Not Found

**Detection**: `FileNotFoundError` when executing Helm commands

**Handling**:
```python
except FileNotFoundError:
    error_message = "Helm command not found. Please ensure Helm is installed and in PATH."
    severity = "critical"
    blocking_issues.append(error_message)
```

**Result**: Critical blocking issue, cannot proceed

#### 2. Validation Timeout

**Detection**: `subprocess.TimeoutExpired`

**Handling**:
```python
except subprocess.TimeoutExpired:
    error_message = "Helm lint validation timed out after 60 seconds"
    severity = "error"
    blocking_issues.append(error_message)
```

**Timeouts**:
- `helm_lint_validator`: 60 seconds
- `helm_template_validator`: 60 seconds
- `helm_dry_run_validator`: 120 seconds (longer due to cluster communication)

#### 3. Validation Failure

**Detection**: Non-zero exit code from Helm command

**Handling**:
1. Parse error output (stderr/stdout)
2. Determine severity (error/warning/info)
3. Create `ValidationResult` object
4. Increment retry count
5. If retry count < 2: Agent attempts fix
6. If retry count >= 2: Call `ask_human`

#### 4. YAML Parsing Errors

**Detection**: `yaml.YAMLError` when parsing rendered templates

**Handling**:
```python
try:
    yaml.safe_load_all(result.stdout)
    yaml_valid = True
except yaml.YAMLError as e:
    yaml_valid = False
    yaml_error = str(e)
    # Agent attempts to fix using edit_file
```

#### 5. Unexpected Exceptions

**Detection**: Any unhandled exception

**Handling**:
```python
except Exception as e:
    error_message = f"Unexpected error during helm lint validation: {str(e)}"
    severity = "error"
    blocking_issues.append(error_message)
    # Log error for debugging
```

### Error Recovery Strategies

1. **Auto-Fix**: Use `edit_file` for fixable errors
2. **Retry**: Re-run validation after fix (max 2 attempts)
3. **Escalate**: Call `ask_human` after max retries
4. **Document**: Add to `blocking_issues` for user awareness

---

## Best Practices

### For Agent Behavior

1. **Always Validate in Order**: lint → template → dry-run
2. **Fix Issues Incrementally**: Fix one type, re-validate, then move to next
3. **Use File Tools for Inspection**: Read files before attempting fixes
4. **Document Fixes**: Note what was fixed in `validation_results`
5. **Set Appropriate Severity**: Use "error", "warning", or "info" based on impact
6. **Update State Properly**: Always use `add` reducer for accumulative fields

### For Self-Healing

1. **Check Retry Counts**: Always check `validation_retry_counts` before retrying
2. **Read Before Fixing**: Use `read_file` to understand error context
3. **Be Specific in Fix Instructions**: Provide clear instructions to `edit_file`
4. **Verify Fixes**: Re-run validation after each fix attempt
5. **Don't Retry Indefinitely**: Stop after 2 attempts and escalate

### For Human Escalation

1. **Be Specific**: Questions should be clear and actionable
2. **Provide Context**: Include error details and attempted fixes
3. **Offer Options**: When possible, provide multiple choice options
4. **Don't Retry After HITL**: Once `ask_human` is called, don't retry the same fix
5. **Document Decisions**: Update `blocking_issues` with human-provided solutions

### For State Management

1. **Use Correct Reducers**: `add` for lists, merge for dicts, `add_messages` for messages
2. **Don't Overwrite Accumulative Fields**: Always append, never replace
3. **Reset Retry Counts on Success**: Set retry count to 0 when validation passes
4. **Track Retry Counts Per Validator**: Use validator name as key in `validation_retry_counts`

### For Workspace Management

1. **Use Consistent Paths**: `/workspace/{chart_name}/` for all chart files
2. **Create Subdirectories**: Ensure `templates/` directory exists before writing templates
3. **Verify File Structure**: Use `ls` to verify files after writing
4. **Clean Up**: Consider cleanup after validation (optional, depends on requirements)

---

## Summary

The Generator Agent (Validator Deep Agent) is a sophisticated validation system that:

- ✅ **Validates** Helm charts comprehensively using multiple validation techniques
- ✅ **Self-Heals** common errors autonomously using `edit_file` tool
- ✅ **Escalates** complex issues to humans via `ask_human` tool after max retries
- ✅ **Tracks** retry counts per validator to prevent infinite loops
- ✅ **Integrates** seamlessly with Supervisor Agent via state transformation
- ✅ **Manages** workspace filesystem for chart validation
- ✅ **Reports** detailed validation results and blocking issues

It uses the Deep Agent pattern with built-in file system tools and custom Helm validators to provide autonomous, intelligent chart validation with human oversight when needed.
