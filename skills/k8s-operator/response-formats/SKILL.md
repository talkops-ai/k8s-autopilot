---
name: k8s-response-formats
description: >-
  Response formatting templates for K8s Operator coordinator results.
  Load when you need to present Kubernetes resource lists, pod logs,
  or state-modifying operation results in polished markdown format.
metadata:
  author: talkops.ai
  version: '1.0'
  domain: k8s-operator
---

# K8s Operator — Response Format Templates

## When to Use

Load this skill when the coordinator needs to present results to the user
in a structured, polished format. The coordinator prompt specifies *properties*
of responses (synthesize, use tables, include status markers); this file
provides canonical templates.

## Read-Only Resource List

```markdown
**🔍 Kubernetes Resources** — `{namespace}`

| Name | Kind | Status | Age |
|---|---|---|---|
| {name} | {kind} | ✅ Running | {age} |

*{count} resource(s) found in namespace `{namespace}`.*

---
What would you like to do next?
```

## Pod Logs / Debugging

```markdown
**📋 Pod Logs** — `{pod_name}` (`{namespace}`)

{highlighted_log_summary_with_errors_flagged}

**Detected Issues**: {issue_list_or_none}

---
What would you like to do next?
```

## State-Modifying Operation Result

```markdown
**✅ Operation Complete**

- **Action**: {Created|Scaled|Deleted|Updated}
- **Kind**: `{kind}`
- **Name**: `{resource_name}`
- **Namespace**: `{namespace}`
- **Result**: {status_summary}

{any additional context}

---
What would you like to do next?
```

## Event Summary

```markdown
**📢 Cluster Events** — `{namespace}`

### ⚠️ Warning Events
| Resource | Reason | Message | Count | Last Seen |
|---|---|---|---|---|
| {resource} | {reason} | {message} | {count} | {last_seen} |

### ℹ️ Normal Events
| Resource | Reason | Message | Count | Last Seen |
|---|---|---|---|---|
| {resource} | {reason} | {message} | {count} | {last_seen} |

---
What would you like to do next?
```

## Cluster Health Check

```markdown
**🏥 Cluster Health Check**

**Overall Status**: {✅ Healthy | ⚠️ At Risk | ❌ Critical}

| Component | Status | Details |
|---|---|---|
| Nodes | {status} | {details} |
| System Pods | {status} | {details} |
| Workloads | {status} | {details} |

{recommendations_if_any}

---
What would you like to do next?
```
