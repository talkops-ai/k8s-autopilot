# Goals and rubrics

> How the agent plans, tracks, and verifies multi-step tasks

When you give k8s-autopilot a complex, multi-step task — like setting up monitoring for a cluster, deploying a Helm application, or migrating workloads — the agent doesn't just start making changes. It first proposes a **goal** with concrete acceptance criteria, waits for your approval, and then works through the task while continuously evaluating its own progress against those criteria.

This is the goal and rubric system. Goals define *what needs to be achieved*. Rubrics define *how success is measured*. Together, they ensure the agent doesn't declare "done" until the work actually passes verification.

## How it works — the full picture

Here's the typical flow when you ask the agent to do something non-trivial:

### 1. You describe a task

You type something like:

```
Deploy a Redis cluster with persistence, configure ServiceMonitor for Prometheus, and add network policies
```

### 2. The agent proposes a goal

For multi-step or infrastructure-modifying tasks, the agent automatically drafts a goal. It analyzes your workspace — your repository, existing Kubernetes resources, and context — to create **concrete, verifiable acceptance criteria**.

The proposal looks something like:

> **Objective:** Deploy a Redis cluster with persistence, monitoring, and network isolation
>
> **Acceptance Criteria:**
> - Redis StatefulSet deployed with 3 replicas and PVCs bound
> - Persistent storage configured with appropriate StorageClass
> - ServiceMonitor resource created and scraping Redis exporter metrics
> - NetworkPolicy restricts Redis access to permitted namespaces only

### 3. You review and approve

The goal is presented to you for review. You have three options:

| Action | What happens |
|---|---|
| **Confirm** | Accept the criteria as-is. The goal becomes active. |
| **Edit** | Modify the criteria — add, remove, or change items — then confirm |
| **Reject** | Send feedback, and the agent regenerates the criteria based on your input |

You can also dismiss the proposal entirely if you'd rather work without a formal goal.

### 4. Accepted criteria become the active rubric

This is the key part: once you accept the criteria, they don't just sit there as a passive checklist. They become the **active rubric** — a set of formal acceptance criteria that the system uses to evaluate the agent's work.

The criteria stay active for the entire duration of the task. Every action the agent takes is oriented toward satisfying these criteria.

### 5. The agent executes with a tactical checklist

While the rubric defines *what success looks like*, the agent also maintains a tactical **todo list** for the step-by-step execution plan. This gives you real-time visibility into progress:

- Items are marked `pending`, `in_progress`, or `completed` as the agent works
- The todo list can be revised if the plan changes during execution
- It keeps the agent anchored to the plan, especially during long operations

### 6. Completion is verified, not self-assessed

When the agent believes all criteria are satisfied, it doesn't just mark the goal as done. Instead:

1. The agent **stages** a completion request with evidence
2. A **rubric grader** independently evaluates the work against the accepted criteria
3. If the grader confirms all criteria pass → the goal is marked **complete**
4. If anything fails → the grader sends a specific deficiency report back to the agent
5. The agent fixes the gaps and resubmits

This prevents the agent from giving itself a passing grade on something it missed. The completion is verified by an independent evaluation, not self-assessed.

## Goal lifecycle

A goal moves through these statuses throughout its life:

| Status | What it means | Who controls it |
|---|---|---|
| **Active** | The agent is working toward this goal | Set on acceptance |
| **Blocked** | The agent hit an obstacle and needs your input | Agent reports it |
| **Paused** | You paused the goal — it's preserved but not driving work | You control this |
| **Complete** | All acceptance criteria have been verified | Rubric grader confirms |

The agent can mark a goal `blocked` when it can't proceed without your input, and it can request `complete` — but completion only takes effect after rubric verification. Only you can pause, resume, or clear a goal.

## Rubric grading

The rubric grading system is what makes goals reliable. Here's how it works:

### The grading loop

1. The agent does its work based on the accepted criteria
2. When it believes the work is done, it stages a completion request
3. The rubric grader evaluates every criterion against the actual state of the workspace
4. Failed criteria generate a **deficiency report** — a specific explanation of what's missing
5. The agent receives the report, fixes the gaps, and resubmits
6. This repeats until all criteria pass or the iteration limit is reached

### Cross-model grading

For more reliable results, the grader can use a different model than the one doing the work. For example, if the worker uses Gemini, the grader might use GPT-4.1, or vice versa. This avoids **self-evaluation bias** — the worker can't accidentally overlook its own mistakes.

### Grader safety

The rubric grader is deliberately restricted to evaluation only:

- It can only **read** files that the worker explicitly referenced in its output
- It cannot modify files, run commands, or access external services
- Its purpose is strictly to verify — does the work meet the criteria, or not?

## Amending goals

If you realize mid-task that the criteria need to change, you can amend the goal. Tell the agent what to adjust:

```
Also add a criterion for TLS encryption on the Redis connections
```

The agent revises only the affected criteria while preserving everything else and any progress already made.

## When goals are not needed

Not every request needs a goal. For simple, single-step tasks — like checking a pod's status, reading a log, or answering a question — the agent acts directly without proposing a formal goal. The goal system activates automatically when the agent detects a task that involves multiple steps, architecture changes, or infrastructure modifications.

## Practical example

**You say:**
```
Set up Prometheus monitoring for the production cluster — deploy kube-prometheus-stack 
via Helm, configure ServiceMonitors for all existing deployments, set up alerting rules 
for pod restarts and high memory usage, and create a Grafana dashboard
```

**What happens:**

1. The agent proposes a goal with criteria like:
   - kube-prometheus-stack Helm release deployed and healthy
   - ServiceMonitor resources created for each deployment
   - PrometheusRule configured for pod restart and memory alerts
   - Grafana dashboard ConfigMap created with relevant panels

2. You review and confirm (or edit the criteria)

3. The agent creates a todo list:
   - `[in_progress]` Deploy kube-prometheus-stack Helm chart
   - `[pending]` Create ServiceMonitor resources
   - `[pending]` Configure alerting rules
   - `[pending]` Create Grafana dashboard

4. As it works, it ticks off items and verifies each step

5. When all criteria appear satisfied, the rubric grader verifies:
   - Are the pods actually running?
   - Do the ServiceMonitors match the right selectors?
   - Are the alert rules syntactically valid?
   - Does the dashboard ConfigMap contain the expected panels?

6. If verification passes → goal complete. If not → the agent fixes the gaps.

## Next steps

- **[Operators and Sub-agents](./operators-and-subagents.md)** — How the agent delegates Kubernetes tasks to specialist operators
- **[HITL Governance](./hitl-governance.md)** — Approval modes and safety controls
- **[Configuration](./configuration.md)** — Runtime settings and environment variables
