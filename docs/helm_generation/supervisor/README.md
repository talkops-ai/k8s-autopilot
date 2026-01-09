# Supervisor Agent Documentation

This directory contains comprehensive documentation for the **Supervisor Agent**, which is the central orchestrator for the k8s-autopilot Helm chart generation workflow.

## Documentation Overview

### 📘 Main Documentation

- **[Supervisor Agent Documentation](./supervisor-agent-documentation.md)** - **START HERE**
  - Complete guide to the Supervisor Agent implementation
  - Architecture, state management, state transformation
  - Human-in-the-loop gates and workflow orchestration
  - Tool-based delegation and stream processing
  - Integration with swarms and interrupt handling

## Quick Start

1. **Read the main documentation**: [Supervisor Agent Documentation](./supervisor-agent-documentation.md)
2. **Understand the workflow**: Review the workflow sequence section
3. **Learn about HITL gates**: See how human approvals work
4. **Explore state transformation**: Understand how state flows between swarms

## Key Concepts

### Supervisor Agent Features

- ✅ **Tool-Based Delegation**: Uses `create_agent()` pattern with tool wrappers
- ✅ **Stateless Invocation**: Sub-swarms run statelessly to allow clean parent check-pointing
- ✅ **State Transformation**: Converts state between supervisor and swarm schemas
- ✅ **HITL Gates**: Mandatory approval points (planning review, generation review)
- ✅ **Workflow Orchestration**: Coordinates planning → generation → validation phases
- ✅ **Stream Processing**: Async streaming with generator-safe interrupt handling
- ✅ **Shared Checkpointer**: All agents use same checkpoint store

### Workflow Sequence

```
1. User Request
   ↓
2. transfer_to_planning_swarm() → Planning complete
   ↓
3. transfer_to_template_supervisor() → Generation complete
   ↓
4. request_generation_review() [MANDATORY HITL GATE]
   - Review artifacts
   - Specify workspace directory
   - Approval required
   ↓
5. transfer_to_validator_deep_agent() → Validation complete
   ↓
6. request_human_feedback(mark_deployment_complete=True)
   ↓
7. Workflow Complete
```

### Human-in-the-Loop Gates

**Mandatory Gates**:

1. **Generation Review Gate** (`request_generation_review`)
   - **When**: After generation phase completes
   - **Purpose**: Review artifacts + specify workspace directory
   - **Required**: Cannot proceed to validation without approval

**Optional Gates**:

- Planning review (currently auto-proceeds)
- Deployment approval (handled via final feedback)

### State Transformation

The `StateTransformer` class handles bidirectional conversion:

- **Supervisor → Swarm**: Extracts relevant data, transforms to swarm schema
- **Swarm → Supervisor**: Aggregates results, updates workflow state

**Key Transformations**:

- `supervisor_to_planning()` / `planning_to_supervisor()`
- `supervisor_to_generation()` / `generation_to_supervisor()`
- `supervisor_to_validation()` / `validation_to_supervisor()`

## Current Scope

**⚠️ Important**: Currently, the Supervisor Agent **ONLY supports Helm chart generation**. While the architecture is designed to support broader Kubernetes operations, the current implementation focuses exclusively on:

- ✅ Helm chart planning
- ✅ Helm chart template generation
- ✅ Helm chart validation

Future releases will expand capabilities to include deployment, CI/CD pipelines, and other Kubernetes operations.

## Related Documentation

- **[Planner Agent Documentation](../planner/planner-agent-documentation.md)** - Planning phase
- **[Template Coordinator Documentation](../template/template-coordinator-documentation.md)** - Generation phase
- **[Generator Agent Documentation](../generator/generator-agent-documentation.md)** - Validation phase
- **[Architecture Documentation](../k8s-autopilot-architecture-enhanced.md)** - Overall system architecture

## File Structure

```
docs/supervisor/
├── README.md                              # This file
└── supervisor-agent-documentation.md      # Main documentation (START HERE)
```

## Contributing

When updating Supervisor Agent documentation:

1. Update [supervisor-agent-documentation.md](./supervisor-agent-documentation.md) for implementation changes
2. Update this README if adding new documents or major changes
3. Ensure workflow sequence and HITL gate rules are accurate

## Questions?

For questions about the Supervisor Agent:
- Check the [main documentation](./supervisor-agent-documentation.md)
- Reference [swarm documentation](../planner/), [template](../template/), [generator](../generator/) for phase-specific details
