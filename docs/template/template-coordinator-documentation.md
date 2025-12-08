# Template Coordinator Agent Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [State Management](#state-management)
4. [Node Implementations](#node-implementations)
5. [Tool Execution Flow](#tool-execution-flow)
6. [Phases and Dependencies](#phases-and-dependencies)
7. [Error Handling](#error-handling)
8. [Integration](#integration)
9. [Examples](#examples)

---

## Overview

The **Template Coordinator Agent** (`TemplateSupervisor`) is responsible for generating Helm chart templates from the planning phase output. It orchestrates the execution of multiple specialized tools to create production-ready Kubernetes manifests with proper Helm templating.

### Key Responsibilities

- **Template Generation**: Generate Helm chart templates (Deployment, Service, ConfigMap, etc.)
- **Dependency Management**: Ensure tools execute in the correct order based on dependencies
- **State Coordination**: Manage execution state across multiple tool invocations
- **Error Recovery**: Handle tool failures with retry logic and graceful degradation
- **Chart Assembly**: Aggregate all generated templates into a complete Helm chart structure

### Technology Stack

- **Framework**: LangGraph StateGraph
- **State Management**: `GenerationSwarmState`
- **Tool Pattern**: LangChain tools with `@tool` decorator
- **LLM Integration**: Configurable LLM providers via `LLMProvider`
- **Checkpointing**: Memory-based checkpointing for state persistence

---

## Architecture

The Template Coordinator follows a **coordinator pattern** with specialized nodes:

```
┌─────────────────────────────────────────────────────────────┐
│                    Template Coordinator                      │
│                  (TemplateSupervisor)                        │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
        ▼                     ▼
┌──────────────┐      ┌──────────────┐
│Initialization│      │  Coordinator │
│    Node      │─────▶│     Node     │
└──────────────┘      └──────┬───────┘
                             │
                ┌────────────┼────────────┐
                │            │            │
                ▼            ▼            ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │   Tool   │  │Aggregator│  │  Error   │
        │ Executor │  │   Node   │  │ Handler  │
        │   Node   │  └──────────┘  │   Node   │
        └────┬─────┘                 └────┬───┘
             │                                │
             └──────────────┬─────────────────┘
                            │
                            ▼
                    ┌──────────────┐
                    │  13 Tools    │
                    │  (See Tools  │
                    │  Section)    │
                    └──────────────┘
```

### Graph Structure

```python
graph = StateGraph(GenerationSwarmState)

# Nodes
graph.add_node("initialization", initialization_node)
graph.add_node("coordinator", coordinator_node)
graph.add_node("tool_executor", tool_executor_node)
graph.add_node("aggregator", aggregator_node)
graph.add_node("error_handler", error_handler_node)

# Edges
START → initialization → coordinator
coordinator → tool_executor | aggregator | error_handler
tool_executor → coordinator (loop back)
aggregator → END
error_handler → coordinator | END
```

---

## State Management

### State Schema: `GenerationSwarmState`

**Location**: `k8s_autopilot/core/state/base.py`

**Key Fields**:

- `planner_output`: Dict[str, Any] - Input from planning phase (read-only)
- `current_phase`: Literal["CORE_TEMPLATES", "CONDITIONAL_TEMPLATES", "DOCUMENTATION", "AGGREGATION", "COMPLETED"]
- `next_action`: str - Name of next tool to execute
- `tools_to_execute`: List[str] - Queue of all tools to execute
- `completed_tools`: List[str] - Tools that have completed successfully
- `pending_dependencies`: Dict[str, List[str]] - Tool dependency mapping
- `generated_templates`: Dict[str, str] - Accumulated template files (filename → content)
- `tool_results`: Dict[str, Any] - Results from each tool execution
- `coordinator_state`: Dict[str, Any] - Internal coordinator state
- `errors`: List[Dict[str, Any]] - Error tracking
- `final_helm_chart`: Dict[str, str] - Final assembled chart
- `final_status`: Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILED", None]

**State Flow**:
```
1. Initialization: Analyze planner_output → determine tools → set dependencies
2. Coordinator: Route to next tool based on phase and dependencies
3. Tool Executor: Execute tool → update generated_templates → mark completed
4. Coordinator: Check if phase complete → transition or continue
5. Aggregator: Assemble final chart structure → set final_status
```

---

## Node Implementations

### 1. Initialization Node

**Purpose**: Analyze planner output and set up execution state.

**Process**:
1. Extract Kubernetes architecture from `planner_output`
2. Identify core resources (Namespace, Deployment, Service)
3. Map auxiliary resources to conditional tools
4. Build tool execution queue with proper ordering
5. Identify tool dependencies

**Key Logic**:
```python
# Check for Namespace in core resources
has_namespace = any(
    resource.get("type") == "Namespace" 
    for resource in core_resources
)

# Map auxiliary resources to tools
RESOURCE_TO_TOOL = {
    "HorizontalPodAutoscaler": "generate_hpa_yaml",
    "PodDisruptionBudget": "generate_pdb_yaml",
    "NetworkPolicy": "generate_network_policy_yaml",
    "Ingress": "generate_traefik_ingressroute_yaml",
    "ConfigMap": "generate_configmap_yaml",
    "Secret": "generate_secret",
    "ServiceAccount": "generate_service_account_rbac"
}

# Build tool queue (ORDER MATTERS)
core_tools = ["generate_helpers_tpl"]
if has_namespace:
    core_tools.append("generate_namespace_yaml")
core_tools.extend(["generate_deployment_yaml", "generate_service_yaml"])
values_tools = ["generate_values_yaml"]  # After all templates
doc_tools = ["generate_readme"]  # Last

tools_to_execute = core_tools + conditional_tools + values_tools + doc_tools
```

**Output**: Initial state with `tools_to_execute`, `pending_dependencies`, `current_phase = "CORE_TEMPLATES"`

### 2. Coordinator Node

**Purpose**: Route execution based on current phase and dependencies.

**Routing Logic**:

#### Phase: CORE_TEMPLATES
```python
if "generate_helpers_tpl" not in completed_tools:
    return {"next_action": "generate_helpers_tpl"}
elif has_namespace and "generate_namespace_yaml" not in completed_tools:
    return {"next_action": "generate_namespace_yaml"}
elif "generate_deployment_yaml" not in completed_tools:
    return {"next_action": "generate_deployment_yaml"}
elif "generate_service_yaml" not in completed_tools:
    return {"next_action": "generate_service_yaml"}
else:
    # All core templates done → transition to CONDITIONAL_TEMPLATES
    return {
        "current_phase": "CONDITIONAL_TEMPLATES",
        "next_action": "check_conditional_tools"
    }
```

#### Phase: CONDITIONAL_TEMPLATES
```python
# Find next conditional tool with dependencies met
for tool in tools_to_execute:
    if tool not in completed_tools and tool not in core/doc tools:
        deps = pending_dependencies.get(tool, [])
        if all(dep in completed_tools for dep in deps):
            return {"next_action": tool}

# All conditional tools done → generate values.yaml
if "generate_values_yaml" not in completed_tools:
    return {"next_action": "generate_values_yaml"}
else:
    # Move to documentation
    return {
        "current_phase": "DOCUMENTATION",
        "next_action": "generate_readme"
    }
```

#### Phase: DOCUMENTATION
```python
if "generate_readme" not in completed_tools:
    return {"next_action": "generate_readme"}
else:
    return {
        "current_phase": "AGGREGATION",
        "next_action": "aggregate_chart"
    }
```

#### Phase: AGGREGATION
```python
return {
    "next_action": "aggregation",
    "coordinator_state": {
        **state.get("coordinator_state", {}),
        "final_status": "SUCCESS"
    }
}
```

**Routing Function**: `route_from_coordinator()`
- Returns "tool_executor" for tool names
- Returns "aggregator" for "aggregate_chart" or "aggregation"
- Returns "error_handler" for "error_handler"

### 3. Tool Executor Node

**Purpose**: Execute a single tool based on `next_action`.

**Process**:
1. Extract tool name from `next_action`
2. Get tool function from `TOOL_MAPPING`
3. Create `MockToolRuntime` to inject state
4. Call tool's underlying coroutine function
5. Update state with tool output
6. Mark tool as completed

**Key Implementation**:
```python
tool_func = TOOL_MAPPING.get(tool_name)
mock_runtime = MockToolRuntime(state)
tool_call_id = f"call_{tool_name}_{uuid.uuid4().hex[:8]}"

# Call underlying function (handles async/sync)
if hasattr(tool_func, '__wrapped__'):
    underlying_func = tool_func.__wrapped__
elif hasattr(tool_func, 'coroutine'):
    underlying_func = tool_func.coroutine
else:
    underlying_func = tool_func.func

result_command = await underlying_func(runtime=mock_runtime, tool_call_id=tool_call_id)

# Update completed_tools
completed_tools = state.get("completed_tools", []) + [tool_name]
```

**Error Handling**: Catches exceptions, logs errors, routes to error_handler

### 4. Aggregator Node

**Purpose**: Assemble final Helm chart structure.

**Process**:
1. Generate `Chart.yaml` from app metadata
2. Organize generated templates into proper directory structure
3. Create final chart dictionary

**File Organization**:
```python
final_helm_chart = {
    "Chart.yaml": chart_yaml,
    "templates/deployment.yaml": deployment_content,
    "templates/service.yaml": service_content,
    "templates/_helpers.tpl": helpers_content,
    "templates/hpa.yaml": hpa_content,  # If generated
    "values.yaml": values_content,
    "README.md": readme_content
}
```

**Output**: `final_helm_chart` dictionary with complete chart structure

### 5. Error Handler Node

**Purpose**: Handle tool execution errors with retry logic.

**Process**:
1. Extract latest error from `errors` list
2. Check retry count against max retries (default: 3)
3. If retries available: increment count and retry same tool
4. If max retries exceeded: skip tool and continue workflow

**Retry Logic**:
```python
retry_count = coordinator_state.get("current_retry_count", 0)
max_retries = coordinator_state.get("max_retries", 3)

if retry_count < max_retries:
    return {
        "coordinator_state": {
            **coordinator_state,
            "current_retry_count": retry_count + 1
        },
        "next_action": tool_name  # Retry same tool
    }
else:
    # Skip tool and continue
    completed_tools = current_completed + [tool_name]
    return {
        "completed_tools": completed_tools,
        "coordinator_state": {
            **coordinator_state,
            "skipped_tools": skipped_tools + [tool_name]
        },
        "next_action": "coordinator"
    }
```

**Routing Function**: `route_from_error_handler()`
- Returns "coordinator" to continue workflow
- Returns `END` if `final_status == "FAILED"`

---

## Tool Execution Flow

### Complete Execution Sequence

```
1. INITIALIZATION
   ├─ Analyze planner_output.kubernetes_architecture
   ├─ Identify core resources (Namespace?, Deployment, Service)
   ├─ Map auxiliary resources to conditional tools
   ├─ Build tools_to_execute queue
   └─ Identify dependencies

2. CORE_TEMPLATES Phase
   ├─ generate_helpers_tpl (FIRST - other templates use helpers)
   ├─ generate_namespace_yaml (if Namespace in core resources)
   ├─ generate_deployment_yaml
   ├─ generate_service_yaml
   └─ Transition to CONDITIONAL_TEMPLATES

3. CONDITIONAL_TEMPLATES Phase
   ├─ Check dependencies for each conditional tool
   ├─ Execute tools with dependencies met:
   │   ├─ generate_hpa_yaml (depends on: deployment)
   │   ├─ generate_pdb_yaml (depends on: deployment)
   │   ├─ generate_network_policy_yaml (depends on: deployment)
   │   ├─ generate_traefik_ingressroute_yaml (depends on: service, helpers)
   │   ├─ generate_configmap_yaml (no dependencies)
   │   ├─ generate_secret (no dependencies)
   │   └─ generate_service_account_rbac (no dependencies)
   ├─ generate_values_yaml (AFTER all templates - collects all template vars)
   └─ Transition to DOCUMENTATION

4. DOCUMENTATION Phase
   ├─ generate_readme (depends on: all templates + values.yaml)
   └─ Transition to AGGREGATION

5. AGGREGATION Phase
   ├─ Generate Chart.yaml
   ├─ Organize file structure
   ├─ Create final_helm_chart dictionary
   └─ Set final_status = "SUCCESS"

6. COMPLETED
   └─ Return final chart to supervisor
```

### Tool Dependency Graph

```
generate_helpers_tpl (no dependencies)
    ↓
generate_namespace_yaml (depends on: helpers)
    ↓
generate_deployment_yaml (depends on: helpers, namespace?)
    ↓
generate_service_yaml (depends on: deployment)
    ↓
generate_hpa_yaml (depends on: deployment)
generate_pdb_yaml (depends on: deployment)
generate_network_policy_yaml (depends on: deployment)
generate_traefik_ingressroute_yaml (depends on: service, helpers)
generate_configmap_yaml (no dependencies)
generate_secret (no dependencies)
generate_service_account_rbac (no dependencies)
    ↓
generate_values_yaml (depends on: ALL templates)
    ↓
generate_readme (depends on: ALL templates + values.yaml)
```

---

## Phases and Dependencies

### Phase 1: CORE_TEMPLATES

**Purpose**: Generate essential templates required for every Helm chart.

**Tools** (execution order):
1. `generate_helpers_tpl` - **MUST BE FIRST** (other templates reference helpers)
2. `generate_namespace_yaml` - If Namespace resource exists (before deployment)
3. `generate_deployment_yaml` - Core workload
4. `generate_service_yaml` - Service discovery

**Dependencies**:
- Deployment depends on helpers (for labels)
- Deployment depends on namespace (if namespace exists)
- Service depends on deployment (for selectors)

**Completion Criteria**: All core tools in `completed_tools`

### Phase 2: CONDITIONAL_TEMPLATES

**Purpose**: Generate optional templates based on planner analysis.

**Conditional Tools**:
- `generate_hpa_yaml` - If HPA enabled in planner
- `generate_pdb_yaml` - If high_availability = true
- `generate_network_policy_yaml` - If network_policies = true
- `generate_traefik_ingressroute_yaml` - If Ingress resource exists
- `generate_configmap_yaml` - If ConfigMap mentioned
- `generate_secret` - If secrets mentioned
- `generate_service_account_rbac` - If RBAC required

**Dependencies**:
- HPA, PDB, NetworkPolicy depend on deployment (for selectors/labels)
- Traefik IngressRoute depends on service and helpers
- ConfigMap, Secret, ServiceAccount have no hard dependencies

**Special Tool**: `generate_values_yaml`
- Runs **AFTER** all templates (core + conditional)
- Collects all `{{ .Values.* }}` references from generated templates
- Ensures values.yaml covers all template variables

**Completion Criteria**: All conditional tools + values.yaml in `completed_tools`

### Phase 3: DOCUMENTATION

**Purpose**: Generate comprehensive documentation.

**Tools**:
- `generate_readme` - Complete README.md with installation, configuration, troubleshooting

**Dependencies**:
- Depends on ALL templates + values.yaml (needs complete information)

**Completion Criteria**: README in `completed_tools`

### Phase 4: AGGREGATION

**Purpose**: Assemble final Helm chart structure.

**Process**:
1. Generate `Chart.yaml` metadata
2. Organize templates into `templates/` directory
3. Place `values.yaml` and `README.md` at root
4. Create final dictionary structure

**Output**: `final_helm_chart` dictionary ready for supervisor

### Dependency Identification

**Method**: `identify_tool_dependencies()`

**Logic**:
```python
dependencies = {}

# Namespace requires helpers
if has_namespace:
    dependencies["generate_namespace_yaml"] = ["generate_helpers_tpl"]

# Deployment requires namespace (if exists) and helpers
if has_namespace:
    dependencies["generate_deployment_yaml"] = ["generate_helpers_tpl", "generate_namespace_yaml"]
else:
    dependencies["generate_deployment_yaml"] = ["generate_helpers_tpl"]

# Service requires deployment (for selectors)
dependencies["generate_service_yaml"] = ["generate_deployment_yaml"]

# HPA/PDB/NetworkPolicy require deployment
if "generate_hpa_yaml" in conditional_tools:
    dependencies["generate_hpa_yaml"] = ["generate_deployment_yaml"]
if "generate_pdb_yaml" in conditional_tools:
    dependencies["generate_pdb_yaml"] = ["generate_deployment_yaml"]
if "generate_network_policy_yaml" in conditional_tools:
    dependencies["generate_network_policy_yaml"] = ["generate_deployment_yaml"]

# Ingress requires service and helpers
if "generate_traefik_ingressroute_yaml" in conditional_tools:
    dependencies["generate_traefik_ingressroute_yaml"] = ["generate_service_yaml", "generate_helpers_tpl"]

# values.yaml depends on ALL templates
all_template_tools = core_tools + conditional_tools
dependencies["generate_values_yaml"] = all_template_tools

# README depends on ALL templates + values.yaml
dependencies["generate_readme"] = all_template_tools + ["generate_values_yaml"]
```

---

## Error Handling

### Error Classification

**Recoverable Errors**:
- Transient LLM failures
- Rate limiting
- Network timeouts
- Temporary validation errors

**Unrecoverable Errors**:
- Invalid planner output structure
- Missing required fields
- Schema validation failures

### Retry Strategy

**Default Configuration**:
- `max_retries`: 3
- Retry same tool on failure
- Increment `current_retry_count` in coordinator_state

**After Max Retries**:
- Mark tool as completed (skipped)
- Add to `skipped_tools` list
- Continue workflow with remaining tools
- Set `final_status = "PARTIAL_SUCCESS"` if some tools skipped

### Error Entry Structure

```python
{
    "tool": "generate_deployment_yaml",
    "error": "LLM rate limit exceeded",
    "timestamp": "2025-01-XXT...",
    "retry_count": 2
}
```

### Error Handler Flow

```
Tool Execution Fails
    ↓
Error Handler Node
    ↓
Check retry_count < max_retries?
    ├─ YES → Increment retry_count → Retry tool
    └─ NO → Skip tool → Continue workflow
```

---

## Integration

### Integration with Main Supervisor

**Location**: `k8s_autopilot/core/agents/supervisor_agent.py`

**Tool**: `transfer_to_template_supervisor`

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
```

**State Transformation**:
1. **Supervisor → Generation**: `StateTransformer.supervisor_to_generation()`
   - Maps `planner_output` → `planner_output`
   - Maps `session_id`, `task_id`
   - Initializes `GenerationSwarmState`

2. **Generation → Supervisor**: `StateTransformer.generation_to_supervisor()`
   - Maps `final_helm_chart` → `helm_chart_artifacts`
   - Updates `workflow_state.generation_complete = True`
   - Updates `active_phase = "validation"`

### Factory Function

**Location**: `k8s_autopilot/core/agents/helm_generator/template/template_coordinator.py`

```python
def create_template_supervisor(
    config: Optional[Config] = None,
    custom_config: Optional[Dict[str, Any]] = None,
    name: str = "template_supervisor",
    memory: Optional[MemorySaver] = None
) -> TemplateSupervisor
```

**Usage**:
```python
template_supervisor = create_template_supervisor(config)
compiled_template_supervisor = template_supervisor.build_graph().compile()
```

---

## Examples

### Example 1: Simple Deployment (No Conditional Tools)

**Planner Output**:
```json
{
  "kubernetes_architecture": {
    "resources": {
      "core": [{"type": "Deployment"}],
      "auxiliary": [{"type": "Service"}]
    }
  }
}
```

**Execution Flow**:
```
1. Initialization
   ├─ Core tools: [helpers, deployment, service]
   ├─ Conditional tools: []
   └─ Dependencies: {deployment: [helpers], service: [deployment]}

2. CORE_TEMPLATES
   ├─ generate_helpers_tpl ✓
   ├─ generate_deployment_yaml ✓
   ├─ generate_service_yaml ✓
   └─ Phase complete

3. CONDITIONAL_TEMPLATES
   ├─ No conditional tools
   └─ generate_values_yaml ✓

4. DOCUMENTATION
   └─ generate_readme ✓

5. AGGREGATION
   └─ Final chart assembled
```

**Final Output**:
```python
{
    "Chart.yaml": "...",
    "templates/_helpers.tpl": "...",
    "templates/deployment.yaml": "...",
    "templates/service.yaml": "...",
    "values.yaml": "...",
    "README.md": "..."
}
```

### Example 2: Complex Deployment (All Conditional Tools)

**Planner Output**:
```json
{
  "kubernetes_architecture": {
    "resources": {
      "core": [
        {"type": "Namespace"},
        {"type": "Deployment"}
      ],
      "auxiliary": [
        {"type": "Service"},
        {"type": "HorizontalPodAutoscaler"},
        {"type": "PodDisruptionBudget"},
        {"type": "NetworkPolicy"},
        {"type": "Ingress"},
        {"type": "ConfigMap"},
        {"type": "Secret"},
        {"type": "ServiceAccount"}
      ]
    }
  }
}
```

**Execution Flow**:
```
1. Initialization
   ├─ Core tools: [helpers, namespace, deployment, service]
   ├─ Conditional tools: [hpa, pdb, networkpolicy, ingressroute, configmap, secret, sa]
   └─ Dependencies: {
        namespace: [helpers],
        deployment: [helpers, namespace],
        service: [deployment],
        hpa: [deployment],
        pdb: [deployment],
        networkpolicy: [deployment],
        ingressroute: [service, helpers],
        values: [all templates],
        readme: [all templates + values]
      }

2. CORE_TEMPLATES
   ├─ generate_helpers_tpl ✓
   ├─ generate_namespace_yaml ✓
   ├─ generate_deployment_yaml ✓
   ├─ generate_service_yaml ✓
   └─ Phase complete

3. CONDITIONAL_TEMPLATES
   ├─ generate_configmap_yaml ✓ (no deps)
   ├─ generate_secret ✓ (no deps)
   ├─ generate_service_account_rbac ✓ (no deps)
   ├─ generate_hpa_yaml ✓ (deployment ✓)
   ├─ generate_pdb_yaml ✓ (deployment ✓)
   ├─ generate_network_policy_yaml ✓ (deployment ✓)
   ├─ generate_traefik_ingressroute_yaml ✓ (service ✓, helpers ✓)
   └─ generate_values_yaml ✓ (all templates ✓)

4. DOCUMENTATION
   └─ generate_readme ✓

5. AGGREGATION
   └─ Final chart assembled
```

**Final Output**:
```python
{
    "Chart.yaml": "...",
    "templates/_helpers.tpl": "...",
    "templates/namespace.yaml": "...",
    "templates/deployment.yaml": "...",
    "templates/service.yaml": "...",
    "templates/hpa.yaml": "...",
    "templates/pdb.yaml": "...",
    "templates/networkpolicy.yaml": "...",
    "templates/ingressroute.yaml": "...",
    "templates/configmap.yaml": "...",
    "templates/secret.yaml": "...",
    "templates/serviceaccount.yaml": "...",
    "templates/rbac.yaml": "...",
    "values.yaml": "...",
    "README.md": "..."
}
```

### Example 3: Tool Execution with Error Recovery

**Scenario**: `generate_hpa_yaml` fails twice, succeeds on third retry

```
1. Coordinator routes to generate_hpa_yaml
2. Tool executor calls generate_hpa_yaml
3. Tool fails (rate limit error)
   ├─ Error logged: {tool: "generate_hpa_yaml", retry_count: 0}
   └─ Routes to error_handler

4. Error Handler
   ├─ retry_count (0) < max_retries (3) ✓
   ├─ Increment retry_count → 1
   └─ Return to coordinator with next_action = "generate_hpa_yaml"

5. Coordinator routes to generate_hpa_yaml (retry)
6. Tool executor calls generate_hpa_yaml
7. Tool fails again (rate limit error)
   ├─ Error logged: {tool: "generate_hpa_yaml", retry_count: 1}
   └─ Routes to error_handler

8. Error Handler
   ├─ retry_count (1) < max_retries (3) ✓
   ├─ Increment retry_count → 2
   └─ Return to coordinator with next_action = "generate_hpa_yaml"

9. Coordinator routes to generate_hpa_yaml (retry)
10. Tool executor calls generate_hpa_yaml
11. Tool succeeds ✓
    ├─ Updates generated_templates["hpa.yaml"]
    ├─ Marks generate_hpa_yaml as completed
    └─ Returns to coordinator

12. Coordinator continues with next tool
```

### Example 4: Tool Skipping After Max Retries

**Scenario**: `generate_network_policy_yaml` fails 3 times, gets skipped

```
1-3. Tool fails 3 times (retry_count: 0 → 1 → 2)

4. Error Handler
   ├─ retry_count (2) < max_retries (3) ✓
   ├─ Increment retry_count → 3
   └─ Retry again

5. Tool fails 4th time
   ├─ Error logged: {tool: "generate_network_policy_yaml", retry_count: 3}
   └─ Routes to error_handler

6. Error Handler
   ├─ retry_count (3) >= max_retries (3) ✗
   ├─ Mark tool as completed (skipped)
   ├─ Add to skipped_tools
   └─ Return to coordinator

7. Coordinator continues with remaining tools
8. Final status: "PARTIAL_SUCCESS" (some tools skipped)
```

---

## Tool Catalog

The coordinator manages **13 tools** organized into categories:

### Core Tools (Always Executed)

1. **generate_helpers_tpl** - Helm helper templates (`_helpers.tpl`)
2. **generate_namespace_yaml** - Namespace resource (conditional on planner)
3. **generate_deployment_yaml** - Deployment/StatefulSet manifest
4. **generate_service_yaml** - Service manifest
5. **generate_values_yaml** - Values.yaml configuration file

### Conditional Tools (Feature-Based)

6. **generate_hpa_yaml** - HorizontalPodAutoscaler (if autoscaling enabled)
7. **generate_pdb_yaml** - PodDisruptionBudget (if HA required)
8. **generate_network_policy_yaml** - NetworkPolicy (if network policies enabled)
9. **generate_traefik_ingressroute_yaml** - Traefik IngressRoute (if Ingress resource)
10. **generate_configmap_yaml** - ConfigMap (if config data needed)
11. **generate_secret** - Secret (if secrets needed)
12. **generate_service_account_rbac** - ServiceAccount + RBAC (if RBAC required)

### Documentation Tools

13. **generate_readme** - README.md documentation

**See separate tool documentation files for detailed information on each tool.**

---

## Best Practices

### For Developers

1. **Tool Ordering**: Always respect dependency order (helpers first, values last)
2. **State Updates**: Tools should return `Command` with state updates
3. **Error Handling**: Tools should catch exceptions and return error messages
4. **Template Variables**: Tools should extract and report all `{{ .Values.* }}` references
5. **Validation**: Tools should validate YAML syntax before returning

### For Operators

1. **Monitor State**: Track `completed_tools` to understand generation progress
2. **Check Errors**: Review `errors` list for tool failures
3. **Verify Output**: Ensure `final_helm_chart` contains all expected files
4. **Handle Skipped Tools**: Review `skipped_tools` if `final_status = "PARTIAL_SUCCESS"`

### For Users

1. **Complete Planning**: Ensure planner output is complete before generation
2. **Review Output**: Check generated templates before deployment
3. **Validate Chart**: Run `helm lint` on generated chart
4. **Test Deployment**: Test chart with `helm template` before applying

---

## Troubleshooting

### Common Issues

1. **Missing Dependencies**: Ensure tools execute in dependency order
2. **Tool Not Found**: Verify tool name exists in `TOOL_MAPPING`
3. **State Not Updating**: Check tool returns `Command` with proper updates
4. **Infinite Loop**: Verify coordinator transitions phases correctly
5. **Missing Templates**: Check if conditional tools were skipped due to errors

### Debugging Tips

1. **Enable Debug Logging**: Set log level to DEBUG for detailed execution logs
2. **Inspect State**: Check `coordinator_state` for phase transitions
3. **Review Dependencies**: Verify `pending_dependencies` mapping is correct
4. **Check Tool Results**: Review `tool_results` for tool execution details
5. **Monitor Errors**: Track `errors` list for failure patterns

---

## References

- **Implementation**: `k8s_autopilot/core/agents/helm_generator/template/template_coordinator.py`
- **Tools**: `k8s_autopilot/core/agents/helm_generator/template/tools/`
- **Prompts**: `k8s_autopilot/core/agents/helm_generator/template/template_prompts.py`
- **State Schema**: `k8s_autopilot/core/state/base.py` (GenerationSwarmState)
- **Integration**: `k8s_autopilot/core/agents/supervisor_agent.py`

---

**Version**: 1.0  
**Last Updated**: 2025-01-XX  
**Status**: Production-Ready
