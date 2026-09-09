# Interactive UI & Communication (A2UI & A2A)

> How k8s-autopilot connects to your interfaces and delivers rich, interactive experiences

Managing Kubernetes clusters requires precision, speed, and clear feedback. Traditional AI assistants communicate strictly through blocks of text or raw terminal output, which quickly becomes overwhelming during critical incidents or complex deployments. 

Instead of forcing you to read walls of text, **k8s-autopilot** delivers a visual, interactive experience directly inside your conversation. It pairs two modern open standards:

- **A2A (Agent-to-Agent Protocol)**: The communication layer that connects k8s-autopilot to your web browser, desktop dashboard, or other tools with smooth, real-time streaming and persistent conversation history.
- **A2UI (Agent-to-User Interface)**: The interface framework that turns the agent's responses into interactive UI components — such as approval cards, live command trackers, checklists, and metric graphs.

---

## 1. How k8s-autopilot Connects: The A2A Protocol

The **Agent-to-Agent (A2A)** protocol is an open industry standard that defines how AI agents communicate with frontends, client applications, and other autonomous systems. 

For you as a user, A2A powers several essential capabilities:

### Automatic discovery
When you launch the TalkOps web interface or connect an external client, it automatically detects k8s-autopilot on the network. The client instantly discovers which operators are available (Helm, Kubernetes, Observability, App Delivery), which skills are loaded, and what capabilities the agent supports — no manual configuration or connection string fiddling required.

### Real-time streaming
You never have to sit and wonder what the agent is doing. A2A streams information in real time as it happens:
- **Thinking and reasoning**: See the agent's strategy and diagnosis as it forms.
- **Live command execution**: Watch commands run against your cluster with immediate feedback.
- **Interactive surfaces**: Visual cards appear in your chat stream right when action is needed.

### Continuous conversation threads
Every conversation is organized into a dedicated thread. If you close your browser, switch between tasks, or restart the server, your conversation history and state are preserved. You can pick up right where you left off at any time.

### Bidirectional interaction
A2A isn't just one-way output from agent to user. When you click **Approve** on an approval card, choose an option from a dropdown, or answer a question, your action is instantly communicated back to the agent so it can immediately proceed with the workflow.

---

## 2. Interactive Surfaces: The A2UI Framework

**A2UI (Agent-to-User Interface)** is an open standard that allows the agent to generate rich, interactive visual components safely inside the chat window. 

Rather than sending raw scripts or unsafe code, the agent generates structured visual blueprints that your browser renders using native, polished UI components.

Here are the primary interactive surfaces you will encounter in k8s-autopilot:

### 1. Human-in-the-Loop Approval Cards
Whenever an operator proposes an action that could change your infrastructure — such as upgrading a Helm release, scaling workloads, or deleting resources — the agent pauses and presents an interactive **Approval Card**:

- **Proposed Action**: A clear, human-readable summary of what the agent plans to do.
- **Risk Level Badge**: Instant visual indicator (`Low`, `Medium`, `High`, or `Critical`) showing potential impact.
- **Parameter Summary**: A clean table highlighting key values (target namespace, chart version, replica count, or image tag).
- **Justification & Evidence**: The diagnostic reasoning explaining why this action is recommended.
- **One-Click Actions**: Simple buttons to **Approve**, **Reject**, or enable automatic approval for the current session.

You maintain full authority: nothing touches your cluster until you click Approve.

### 2. Live Tool Execution Cards
When the agent runs commands — like querying Prometheus, running a Helm check, or inspecting pod status — it displays a live **Tool Execution Card**:

- **Command & Target**: Displays the tool being executed and the target cluster or namespace.
- **Status Indicator**: Animated spinners showing whether the command is `Running`, `Completed`, or encountered an `Error`.
- **Elapsed Duration**: A timer showing exactly how long the operation took in milliseconds.
- **Terminal Output**: Expandable drawer showing the raw stdout and stderr logs for complete auditing and transparency.

### 3. Thinking & Reasoning Blocks
Before taking action, k8s-autopilot analyzes your question, queries the environment, and formulates a plan. 

The agent displays its thought process in a clean, collapsible **Thinking Block**. You can expand this at any time to understand *why* the agent chose a specific approach or what diagnostic steps it is considering.

### 4. Plan & Progress Checklists
For complex, multi-step tasks (such as troubleshooting a failing rollout or onboarding an application to ArgoCD), the agent organizes its work into a **Plan Checklist**:

- Displays each planned step with its current status (`Pending`, `In Progress`, or `Completed`).
- Automatically updates in real time as the agent progresses through the operation.
- Shows version badges if the plan was adjusted based on your feedback.

### 5. Interactive Question Cards
When the agent needs clarification — such as choosing between multiple ingress controllers, selecting a namespace, or picking a rollback revision — it presents an **Interactive Question Card**:

- Provides clickable options or clean input fields instead of asking you to type raw parameters.
- Prevents typos and mistakes by letting you select from known, validated values.

### 6. Execution Walkthroughs
When a multi-step workflow finishes, the agent presents a comprehensive **Execution Walkthrough**:

- A concise narrative summary of what was diagnosed and fixed.
- An outcome badge (`Success`, `Partial`, or `Failed`).
- A counter showing how many tasks were planned versus completed.

---

## 3. Observability Visualizations

When troubleshooting production issues, raw numbers and logs can be difficult to interpret quickly. k8s-autopilot transforms telemetry into clear, interactive visualizations right in your conversation:

### Interactive metric charts
- Line, bar, and area charts displaying CPU, memory, request rates, or error rates over time.
- Tooltips showing exact values when you hover over data points.
- Automatically optimized so you see every peak, drop, and anomaly without slowing down your browser.

### Smart log tables
- Rather than dumping thousands of identical log lines into your chat, the agent groups repetitive logs into common templates with occurrence counters.
- Quick filters to highlight errors, warnings, or specific error messages.

### Trace waterfall timelines
- Visual timelines showing distributed request flows across microservices.
- Highlights bottlenecks and slow services along the request path with color-coded latency bars.

### Alert summaries
- Real-time summaries of active alerts grouped by severity (`Critical`, `Warning`, `Info`).
- One-click access to view alert details or silence recurring notifications.

---

## 4. Built for Safety and Reliability

The combination of A2A and A2UI ensures that k8s-autopilot remains safe, secure, and predictable:

- **No arbitrary code execution**: All UI components are pre-approved and rendered safely by your browser. The AI model cannot inject unverified scripts or modify your application interface.
- **Complete audit trail**: Every action, user approval, command output, and status transition is recorded in the conversation thread.
- **Fail-safe interaction**: If a connection drops, your active approvals and tasks wait safely until you reconnect.

---

## Next steps

- [HITL Governance](./hitl-governance.md) — Learn how approval cards and safety modes protect your cluster
- [Operators and Sub-agents](./operators-and-subagents.md) — Explore the specialized operators that power these workflows
- [Quickstart Guide](./quickstart.md) — Connect to k8s-autopilot and try your first interactive workflow
