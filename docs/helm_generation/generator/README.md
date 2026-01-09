# Generator Agent Documentation

This directory contains comprehensive documentation for the **Generator Agent** (also known as the Validator Deep Agent), which is responsible for validating, self-healing, and ensuring Helm charts are production-ready.

## Documentation Overview

### 📘 Main Documentation

- **[Generator Agent Documentation](./generator-agent-documentation.md)** - **START HERE**
  - Complete guide to the Generator Agent implementation
  - Architecture, prompts, tools, self-healing mechanisms
  - Human-in-the-loop scenarios and integration details
  - Comprehensive workflow and error handling documentation

### 📋 Design Documents (Reference)

These documents provide design context and implementation details:

- **[Executive Summary](./executive-summary.md)** - High-level architecture overview
  - Design decisions and rationale
  - Tool categories and agent patterns
  - Parallel vs sequential execution strategies
  - Human-in-the-loop interrupts

- **[Validator Implementation](./validator-implementation.md)** - Implementation guide
  - Code examples and templates
  - Type definitions and state schemas
  - Tool implementations
  - Agent code examples

- **[Implementation Gap Analysis](./implementation-gap-analysis.md)** - Current vs. documented state
  - What's implemented vs. what's documented
  - Missing features and tools
  - Coverage analysis and recommendations

- **[DeepAgent Built-in Tools Analysis](./deepagent-builtin-tools-analysis.md)** - Built-in tools guide
  - File system tools (ls, read_file, write_file, edit_file)
  - Planning tools (write_todos)
  - Backend configuration (FilesystemBackend)
  - Migration strategies

- **[Correct Deep Agent Implementation](./correct-create-deep-agent.md)** - API reference
  - Official `create_deep_agent` API
  - Correct import and usage patterns
  - Integration examples

## Quick Start

1. **Read the main documentation**: [Generator Agent Documentation](./generator-agent-documentation.md)
2. **Understand the architecture**: Review the [Executive Summary](./executive-summary.md)
3. **Check implementation status**: See [Implementation Gap Analysis](./implementation-gap-analysis.md)
4. **Reference design docs**: Use other documents as needed for specific topics

## Key Concepts

### Generator Agent Features

- ✅ **Deep Agent Pattern**: ReAct (Reasoning → Action → Observation) for autonomous problem-solving
- ✅ **Built-in File Tools**: Automatic access to `ls`, `read_file`, `write_file`, `edit_file`
- ✅ **Custom Helm Validators**: `helm_lint_validator`, `helm_template_validator`, `helm_dry_run_validator`
- ✅ **Self-Healing**: Automatically fixes common errors (indentation, deprecated APIs, missing fields)
- ✅ **Human-in-the-Loop**: Uses `ask_human` tool to pause and request assistance
- ✅ **Retry Logic**: Tracks retry counts (max 2) before escalating to human

### Validation Workflow

```
1. Receive chart from Supervisor Agent
2. Write chart files to workspace filesystem
3. Run validations sequentially:
   - helm_lint_validator (syntax)
   - helm_template_validator (YAML rendering)
   - helm_dry_run_validator (cluster compatibility)
4. Fix errors autonomously using edit_file
5. Escalate to human via ask_human if max retries reached
6. Return validation results to Supervisor Agent
```

### Self-Healing Scenarios

The agent can autonomously fix:
- **YAML Indentation Errors** (confidence: 0.95)
- **Deprecated API Versions** (confidence: 0.90)
- **Missing Required Fields** (confidence: 0.85)
- **Invalid Values** (confidence: 0.70)

### Human Escalation Scenarios

The agent calls `ask_human` when:
1. **After 2 failed retry attempts** - Prevents infinite loops
2. **Missing critical information** - Version numbers, domain names, etc.
3. **Ambiguous errors** - Unclear error messages
4. **Trade-off decisions** - Multiple valid approaches
5. **Cluster-specific issues** - RBAC, admission controllers, quotas

## Implementation Status

### ✅ Implemented

- Deep Agent pattern with `create_deep_agent`
- Built-in file system tools (ls, read_file, write_file, edit_file)
- Custom Helm validation tools (lint, template, dry-run)
- Self-healing with retry logic (max 2 retries)
- Human-in-the-loop via `ask_human` tool
- State persistence with LangGraph checkpointing
- Workspace management with FilesystemBackend

### ⚠️ Partially Implemented

- Values schema validation (not implemented)
- Security scanning (not implemented)
- Test generation (not implemented)
- ArgoCD configuration (not implemented)

### ❌ Not Implemented

- Parallel execution (currently sequential)
- Specialized agent swarm (single Deep Agent instead)

See [Implementation Gap Analysis](./implementation-gap-analysis.md) for detailed coverage analysis.

## Related Documentation

- **[Template Coordinator Documentation](../template/template-coordinator-documentation.md)** - Previous phase (chart generation)
- **[Planner Agent Documentation](../planner/planner-agent-documentation.md)** - Initial phase (planning)
- **[Supervisor Agent Documentation](../supervisor/)** - Orchestration layer

## File Structure

```
docs/generator/
├── README.md                              # This file
├── generator-agent-documentation.md      # Main documentation (START HERE)
├── executive-summary.md                  # Design overview
├── validator-implementation.md           # Implementation guide
├── implementation-gap-analysis.md        # Current vs. documented state
├── deepagent-builtin-tools-analysis.md   # Built-in tools guide
└── correct-create-deep-agent.md         # API reference
```

## Contributing

When updating Generator Agent documentation:

1. Update [generator-agent-documentation.md](./generator-agent-documentation.md) for implementation changes
2. Update [implementation-gap-analysis.md](./implementation-gap-analysis.md) for feature coverage
3. Update this README if adding new documents or major changes

## Questions?

For questions about the Generator Agent:
- Check the [main documentation](./generator-agent-documentation.md)
- Review [implementation status](./implementation-gap-analysis.md)
- Reference [design documents](./executive-summary.md) for architecture decisions
