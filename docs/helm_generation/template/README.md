# Template Coordinator Agent Documentation

Complete documentation for the Template Coordinator Agent and all associated tools.

## Documentation Index

### Main Documentation

1. **[Template Coordinator Documentation](./template-coordinator-documentation.md)**
   - Architecture overview
   - Node implementations
   - State management
   - Execution flow
   - Error handling
   - Integration guide

### Tool Documentation

2. **[Core Tools](./tools-core-tools.md)**
   - `generate_helpers_tpl` - Helm helper templates
   - `generate_namespace_yaml` - Namespace resource
   - `generate_deployment_yaml` - Deployment/StatefulSet
   - `generate_service_yaml` - Service manifest
   - `generate_values_yaml` - Values.yaml configuration

3. **[Conditional Tools](./tools-conditional-tools.md)**
   - `generate_hpa_yaml` - HorizontalPodAutoscaler
   - `generate_pdb_yaml` - PodDisruptionBudget
   - `generate_network_policy_yaml` - NetworkPolicy
   - `generate_traefik_ingressroute_yaml` - Traefik IngressRoute
   - `generate_configmap_yaml` - ConfigMap
   - `generate_secret` - Secret
   - `generate_service_account_rbac` - ServiceAccount + RBAC

4. **[Documentation Tools](./tools-documentation-tools.md)**
   - `generate_readme` - README.md documentation

5. **[Tools Quick Reference](./tools-quick-reference.md)**
   - Quick reference for all 13 tools
   - Execution order
   - Dependencies matrix
   - Common patterns

### Specialized Documentation

6. **[Traefik Comprehensive Guide](./traefik-comprehensive-guide.md)** ⭐ NEW
   - Complete Traefik IngressRoute documentation
   - Why Traefik vs. standard Ingress
   - Traefik architecture and concepts
   - Matcher syntax guide
   - Middleware system
   - Advanced load balancing
   - TLS configuration
   - Migration guide
   - Examples and best practices
   - Troubleshooting

### Reference Documentation

7. **[Coordinator Agent Architecture](./coordinator-agent-architecture.md)** (May be outdated)
   - Detailed architecture diagrams
   - State machine flows
   - Handoff mechanisms

8. **[Helm Template Agent Architecture](./helm-template-agent-architecture.md)** (May be outdated)
   - Technical architecture details
   - Tool definitions with schemas
   - Prompt engineering

9. **[Conditional Tools Complete](./conditional-tools-complete.md)** (May be outdated)
   - Detailed tool definitions
   - System prompts
   - User prompts

---

## Quick Start

### Understanding the Flow

1. **Read**: [Template Coordinator Documentation](./template-coordinator-documentation.md)
   - Understand the coordinator pattern
   - Learn about phases and dependencies
   - See execution flow examples

2. **Explore Tools**: [Core Tools](./tools-core-tools.md) → [Conditional Tools](./tools-conditional-tools.md)
   - Understand what each tool does
   - Learn input/output schemas
   - See example outputs

3. **Reference**: [Tools Quick Reference](./tools-quick-reference.md)
   - Quick lookup for tool details
   - Dependencies matrix
   - Execution order

### Key Concepts

- **Coordinator Pattern**: Routes execution based on state and dependencies
- **Phases**: CORE_TEMPLATES → CONDITIONAL_TEMPLATES → DOCUMENTATION → AGGREGATION
- **Dependencies**: Tools execute only when dependencies are met
- **Error Recovery**: Retry logic with graceful degradation
- **State Management**: `GenerationSwarmState` tracks execution progress

---

## Architecture Overview

```
Planner Output
    ↓
Initialization Node
    ├─ Analyze planner output
    ├─ Identify required tools
    └─ Build dependency graph
    ↓
Coordinator Node (Loop)
    ├─ Check current phase
    ├─ Find next tool (dependencies met)
    └─ Route to tool executor
    ↓
Tool Executor Node
    ├─ Execute tool
    ├─ Update state
    └─ Return to coordinator
    ↓
Aggregator Node
    ├─ Assemble final chart
    └─ Generate Chart.yaml
    ↓
Final Helm Chart
```

---

## Tool Execution Summary

**Total Tools**: 13

**Core Tools** (5): Always executed
- helpers, namespace (conditional), deployment, service, values

**Conditional Tools** (7): Feature-based
- HPA, PDB, NetworkPolicy, IngressRoute, ConfigMap, Secret, ServiceAccount

**Documentation Tools** (1): Last
- README

---

## Common Use Cases

### Simple Deployment
- Tools: helpers, deployment, service, values, readme
- Duration: ~5 tool executions

### Production Deployment
- Tools: All core + HPA + PDB + NetworkPolicy + IngressRoute + ConfigMap + Secret + SA + values + readme
- Duration: ~13 tool executions

### High Availability Deployment
- Tools: All core + HPA + PDB + NetworkPolicy + values + readme
- Duration: ~10 tool executions

---

## Integration Points

### With Planner Agent
- **Input**: `planner_output` from planning phase
- **Contains**: Parsed requirements, architecture design, resource estimation, scaling strategy

### With Supervisor Agent
- **Tool**: `transfer_to_template_supervisor`
- **State Transformation**: Supervisor ↔ Generation state mapping
- **Output**: `helm_chart_artifacts` dictionary

---

## Best Practices

1. **Tool Ordering**: Always respect dependency order
2. **Error Handling**: Tools should catch exceptions and return errors
3. **State Updates**: Tools must return `Command` with state updates
4. **Validation**: Tools should validate YAML syntax before returning
5. **Template Variables**: Tools should extract and report all `{{ .Values.* }}` references

---

## Troubleshooting

See [Template Coordinator Documentation - Troubleshooting](./template-coordinator-documentation.md#troubleshooting) section.

Common issues:
- Missing dependencies
- Tool not found
- State not updating
- Invalid YAML
- Missing template variables

---

## References

- **Implementation**: `k8s_autopilot/core/agents/helm_generator/template/template_coordinator.py`
- **Tools**: `k8s_autopilot/core/agents/helm_generator/template/tools/`
- **Prompts**: `k8s_autopilot/core/agents/helm_generator/template/template_prompts.py`
- **State Schema**: `k8s_autopilot/core/state/base.py` (GenerationSwarmState)

---

**Version**: 1.0  
**Last Updated**: 2025-01-XX  
**Status**: Production-Ready
