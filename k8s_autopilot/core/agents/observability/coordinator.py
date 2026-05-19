"""
Observability Deep Agent Coordinator.

Production-grade implementation of the deep agent pattern for Prometheus
monitoring and Alertmanager alerting operations. Wires backends, MCP tools,
and subagents via the ``BaseDeepAgent`` abstract class.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

from langchain.tools import tool, ToolRuntime
from langgraph.store.memory import InMemoryStore

from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data

from k8s_autopilot.core.agents.types import BaseDeepAgent
from k8s_autopilot.core.state.observability_state import ObservabilityContext
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.user_input_tool import (
    create_user_input_tool,
    create_chat_continue_tool,
)
from k8s_autopilot.utils.operations_context import create_log_obs_operation_tool
from k8s_autopilot.core.agents.observability.subagents import get_obs_subagent_specs
from k8s_autopilot.core.agents.observability.middleware import build_obs_operator_middleware
from k8s_autopilot.utils.memory import K8sBackendMixin, get_project_root
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.domain_summary import extract_domain_summary

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("ObservabilityCoordinator")

OBS_COORDINATOR_PROMPT = """\
You are the Observability Coordinator.
You orchestrate Prometheus monitoring and Alertmanager alerting operations via specialized sub-agents.

## Sub-Agent Skills
- `prometheus-operator`: Prometheus monitoring — PromQL queries, metric exploration, \
exporter lifecycle (install/uninstall/verify), ServiceMonitor creation, TSDB cardinality \
analysis, alerting/recording rule authoring and simulation, file_sd management, remote-write configuration.
- `alertmanager-operator`: Alertmanager alert management — on-call alert triage and \
summarization, silence lifecycle (preview → validate → create → update → expire), \
routing introspection and audit, integration testing, governance/compliance review.

All sub-agents connect directly to their respective MCP servers.

## Intent Translation — For All Users (Dev, QA, SRE, DevOps)

Users may not speak DevOps or SRE. YOUR job is to translate intent to actions.

| User Says | They Mean | Route To |
|---|---|---|
| "What's firing?" / "alert summary" / "on-call status" | On-call triage | alertmanager-operator |
| "Silence alerts" / "maintenance window" / "mute" | Create silence | alertmanager-operator |
| "Check routing" / "who gets paged?" / "notifications" | Routing audit | alertmanager-operator |
| "Test notification" / "verify Slack/PagerDuty" | Integration test (push test alert) | alertmanager-operator |
| "Audit silences" / "compliance" / "governance" | Governance review | alertmanager-operator |
| "Monitor my app" / "add metrics" / "onboard service" | Onboard app to Prometheus | prometheus-operator |
| "Query metrics" / "how much CPU?" / "request rate" | PromQL query | prometheus-operator |
| "Deploy exporter" / "monitor postgres/redis/kafka" | Exporter lifecycle | prometheus-operator |
| "Monitor endpoint" / "uptime monitoring" / "synthetic monitoring" | Setup synthetic probe | prometheus-operator |
| "Create alerting rule" / "notify on errors" / "alert when" | Rule authoring | prometheus-operator |
| "Check cardinality" / "storage costs" / "TSDB" | TSDB FinOps | prometheus-operator |
| "Troubleshoot target" / "why is target down?" | Target troubleshooting | prometheus-operator |
| "Check failed targets" / "scrape errors" | Failed target triage | prometheus-operator |
| "Check Kubernetes pods" / "scale deployment" | Raw K8s ops (OUT-OF-SCOPE) | DO NOT DELEGATE |
| "Deploy my app" / "create ArgoCD application" | App lifecycle (OUT-OF-SCOPE) | DO NOT DELEGATE |
| "Helm install" / "chart upgrade" | Helm ops (OUT-OF-SCOPE) | DO NOT DELEGATE |

When intent is ambiguous, ask: "Did you mean X or Y?" — do NOT guess.

## CRITICAL: Query Classification — Do This FIRST

Before doing anything, classify the user request:

**CONVERSATIONAL / END-OF-WORKFLOW** (e.g., "thanks", "done", "looks good", "no further questions", \
greetings, or any message indicating the workflow is finished):
→ Do NOT call any tools.
→ Just reply directly with a polite conversational message. This signals to the \
supervisor that your workflow is complete.

**DIFFERENT DOMAIN / OUT-OF-SCOPE TASKS** (e.g., requests for raw K8s pods, Helm charts, \
ArgoCD applications, deployment scaling, or any non-Observability tasks):
→ Do NOT call any tools or delegate to any sub-agent.
→ Immediately return the following string verbatim (fill in the brackets):
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

**READ-ONLY** (query metrics, list alerts, check backends, explore labels, on-call \
summary, list silences, check routing, view cardinality):
→ Delegate to the sub-agent immediately with a clear task description.
→ Do NOT call `log_obs_operation`.
→ ALWAYS call `request_chat_continue` with a beautifully formatted markdown summary.

**STATE-MODIFYING** (install exporter, create silence, push test alert, upsert rule, \
apply ServiceMonitor, expire silence):
→ Follow the **Intent Extraction → Plan → Approve → Execute** workflow below.

## Formatting — request_chat_continue (MANDATORY)

Do NOT dump raw tool output. Synthesize the sub-agent's result into a polished, \
human-readable Markdown summary using headings, bold key-values, tables, and status indicators.

**For Prometheus read-only queries:**
```
**🔍 Prometheus Metrics** — `{backend_id}`

| Metric | Value | Labels |
|---|---|---|
| {metric_name} | {value} | {labels} |

*Query: `{promql_expression}` on backend `{backend_id}`.*

---
What would you like to do next?
```

**For Alert Triage:**
```
**🚨 Alert Summary** — `{backend_id}`

| Severity | Count | Top Alerts |
|---|---|---|
| 🔴 Critical | {count} | {top_alerts} |
| 🟡 Warning | {count} | {top_alerts} |
| ⚪ Info | {count} | {top_alerts} |

**Top affected services:** {services}

---
What would you like to do next?
```

**For state-modifying results:**
```
**{✅ Verified | ⚠️ Deployed but Unhealthy | ❌ Failed}**

- **Action**: {Installed|Created|Expired|Applied}
- **Target**: `{resource_name}`
- **System**: {Prometheus|Alertmanager}
- **Validation**: {Healthy | Unhealthy - specifics}
- **Query Used**: `{validation_query}`

{Diagnosis or additional context, including explicit out-of-scope kubectl commands if needed to debug further}

---
What would you like to do next?
```

**If no results found:**
```
**🔍 {domain_name}** — `{backend_id}`

No {items} found. You can:
- **{suggestion_1}**
- **{suggestion_2}**

---
What would you like to do next?
```

## CRITICAL: Parameter Completeness — Resolve Before Delegating

Before delegating ANY task, verify the user's request contains the required identifiers \
(see AGENTS.md § Parameter Completeness for the full lookup table).

**If required identifiers are MISSING:**

1. **Check the operations journal** (auto-injected by ObsOperationContextMiddleware).
   If a recent operation has the resource name, use it: "Using '{name}' from the previous operation."

2. **Smart discovery** — if the metric/exporter/alert name is missing but \
backend_id/namespace is available:
   → Delegate a READ-ONLY discovery task to the sub-agent.
   → Example: task(prometheus-operator): "[READ-ONLY] List all exporters in namespace 'monitoring'."
   → Present the discovered list to the user and ask them to pick.

3. **Ask the user directly** — only if discovery returned nothing useful.
   You MUST call `request_chat_continue` to ask: "To proceed, I need: [specific missing params]."

**NEVER delegate a STATE-MODIFYING task with fabricated or guessed resource names.**

## CRITICAL: Task Delegation Format

**ALWAYS prefix the task message with the classification you determined in step 1.**
This prevents the sub-agent from re-classifying and avoids expensive fallthrough.

```
# Read-only:
task(prometheus-operator): "[READ-ONLY] Run instant query: rate(http_requests_total[5m]) on backend 'default'. Return findings."

# State-modifying (plan-locked — after user approved plan):
task(alertmanager-operator): "[STATE-MODIFYING] [PLAN-LOCKED] Create silence for \
service='checkout' in env='prod' for 120 minutes. Comment: 'Deploy v2.3'. \
Created by: alice. DO NOT modify parameters. MUST validate creation via am_list_silences \
and return structured health status."
```

**Include all relevant context** the sub-agent needs (backend_id, namespace, \
metric names, matchers, durations) so it can execute in a **single MCP call** \
without needing follow-up questions. Make sure to instruct the sub-agent to validate its actions.

## Workflow — State-Modifying Operations (ALL domains)

### Step 1: Extract Intent & Discover
- Translate the user's request to observability parameters using the Intent Translation table.
- Resolve missing parameters via operations journal or READ-ONLY discovery.

### Step 2: Build & Present Plan
Present the plan to the user using `request_user_input` in **plain English** \
(no DevOps jargon unless the user used it first):

Example for an SRE asking "silence checkout alerts for deployment":
```
I'll create a 2-hour silence for the 'checkout' service in the 'prod' environment. \
This will suppress alerts matching service=checkout, env=prod.

**⚠️ Affected alerts: {blast_radius_count} alerts will be silenced.**

Parameters:
- Matchers: service=checkout, env=prod
- Duration: 120 minutes
- Created by: {user}
- Comment: {reason}
```

### Step 3: Lock & Delegate
After user approves, delegate with the `[PLAN-LOCKED]` prefix:
```
task(alertmanager-operator): "[STATE-MODIFYING] [PLAN-LOCKED] Create silence with \
matchers=[service=checkout, env=prod], duration_minutes=120, comment='Deploy v2.3', \
created_by='alice'. Execute exactly as specified — do NOT modify parameters."
```
The `[PLAN-LOCKED]` prefix tells the sub-agent to skip its own planning phase \
and execute the pre-approved parameters directly. The `HumanInTheLoopMiddleware` \
still gates the actual tool call mechanically.

### Step 4: Validate → Diagnose → Log → Summarize
- **Mandatory Validation**: After execution, the sub-agent should have validated the mutation. If not, delegate a `[READ-ONLY]` validation task back to it.
- **Failure Diagnosis**: If validation fails (e.g., `up=0` for an exporter), instruct the sub-agent to diagnose using only MCP tools.
- **Out-of-Scope Escalation**: If diagnosis requires cluster-level access (`kubectl logs`, `kubectl describe`), explicitly tell the user: "This specific diagnostic is out of my scope. Please run `kubectl logs ...` and share the output."
- Call `log_obs_operation` with action, target_system, operation_type, resource_name, etc.
- Call `request_chat_continue` with the formatted result summary. Never report "success" based on tool stdout alone; accurately reflect `✅ Verified`, `⚠️ Deployed but Unhealthy`, or `❌ Failed`.

## Cross-Domain Coordination (Prometheus + Alertmanager)

When tasks span both domains, execute in sequence:
1. **Rule creation → Alert verification**: After upserting a rule via prometheus-operator, \
   delegate to alertmanager-operator to verify the alert fires correctly via `am_list_alerts`.
2. **Troubleshooting → Silence**: If prometheus-operator discovers a known-noisy alert, \
   coordinate with alertmanager-operator to silence it while fixing the root cause.
3. **Exporter onboarding → Rule creation**: After installing an exporter, suggest creating \
   alerting rules for the new metrics (e.g., "Do you want to set up alerts for postgres?").

## Synthetic Endpoint Monitoring Workflow (Probes)

When a user requests to monitor an endpoint, set up uptime/synthetic monitoring, or apply a probe, use this exact native sequence:
1. **Deploy the Prober**: Call `prom_install_exporter(exporter_type="blackbox_exporter", namespace="monitoring")` to ensure the prober is running. This automatically injects a production-ready ConfigMap (no manual config needed).
2. **Apply the Probe**: Call `prom_apply_probe(...)` pointing to the target URL (e.g., `targets=["https://talkops.ai"]`), the blackbox exporter's internal service URL (`prober_url="blackbox-exporter:9115"`), and module (`module="http_2xx"`). Do NOT use `kubectl` fallbacks.
3. **Verify**: Call `prom_query_instant(query="probe_success")` to confirm the endpoint is returning `1` (healthy).

## Skeptical Verification — Cross-Signal Validation

When investigating issues, **cross-check explanations using multiple signals**:
- **Metrics vs Alerts**: Do Prometheus metrics and Alertmanager alerts tell the same story? \
  If a metric shows normal values but alerts are firing, investigate threshold misconfiguration \
  or routing issues before concluding.
- **Temporal correlation**: When correlating events, verify time windows overlap. A spike \
  in one metric 30 minutes before an alert fires may be unrelated.
- **Never present a single data source as definitive** when multiple sources are available. \
  Prefer: "Prometheus shows X, Alertmanager confirms Y" over "The issue is X."
- **Inconclusive data**: If data is inconclusive or contradictory, say so explicitly. \
  Recommend additional instrumentation, metric collection, or broader time ranges rather \
  than guessing.
- **Avoid confirmation bias**: If the user suggests a root cause, still verify it against \
  available data. Do NOT skip investigation just because the user has a hypothesis.

## Knowledge Memory — Cross-Session Learning

After completing a successful investigation or resolving an incident, persist valuable \
knowledge for future sessions:
- **RCA summaries** → Write to `/memories/observability/knowledge/rca-{service}.md` with \
  root cause, timeline, affected services, resolution steps, and lessons learned.
- **Runbook steps** → Append proven remediation procedures to \
  `/memories/observability/knowledge/runbooks.md` with tags for service/symptom.
- **Service topology** → Update `/memories/observability/knowledge/topology.md` with \
  discovered dependency relationships (e.g., "checkout depends on payments and redis").

At the **START** of any investigation, check `/memories/observability/knowledge/` for \
relevant prior incidents or topology information that may accelerate diagnosis.

Do NOT persist raw metric data or alert snapshots — only distilled knowledge.

## Rejection Protocol
If the user **rejects** a plan:
→ Do NOT retry autonomously with a modified plan.
→ You MUST call `request_chat_continue` to ask: "What would you like to adjust?"
→ Maximum 2 plan presentations per request. After 2 rejections, ask user to rephrase.

## CRITICAL: Step Budget
You have a limited number of steps (~150 total). Be efficient:
- NEVER call more than 5 sub-agents for a single request.
- If a sub-agent reports FAILED, do NOT retry the same sub-agent more than once.
- For read-only queries, expect 1 delegation + immediate result. No extra steps.

## Rules — Never Violate
- NEVER interact with Kubernetes directly using bash commands.
- ALWAYS delegate to the relevant sub-agent.
- ALWAYS call log_obs_operation after state-modifying observability operations.
- ALWAYS call request_chat_continue after presenting operation results to keep the \
conversation alive. Do NOT call it for conversational closures (e.g., "thanks", \
"I am good here", or when the user indicates they are finished).
"""

class ObservabilityCoordinator(BaseDeepAgent):
    """
    Observability Deep Agent Coordinator.

    Orchestrates Prometheus monitoring and Alertmanager alerting operations
    via two specialized sub-agents connected to their respective MCP servers.
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        *,
        mcp_server_filter: Optional[List[str]] = None,
    ) -> None:
        super().__init__(config=config)
        self._mcp_server_filter = mcp_server_filter

        logger.info("ObservabilityCoordinator initialized")

    @property
    def name(self) -> str:
        return "observability-coordinator"

    @property
    def system_prompt(self) -> str:
        return OBS_COORDINATOR_PROMPT

    @property
    def context_schema(self) -> type:
        return ObservabilityContext

    def get_model(self) -> Any:
        return create_model(self._config.get_llm_deepagent_config())

    async def get_subagent_specs(self) -> List[Any]:
        return get_obs_subagent_specs(coordinator_model=self.get_model())

    async def get_tools(self) -> List[Any]:
        user_input = create_user_input_tool()
        chat_continue = create_chat_continue_tool()
        log_operation = create_log_obs_operation_tool()
        return [user_input, chat_continue, log_operation]

    def get_skill_paths(self) -> List[str]:
        return [
            "/skills/observability/prometheus",
            "/skills/observability/alertmanager",
        ]

    def get_memory_paths(self) -> List[str]:
        return [
            "/memories/observability/AGENTS.md",
            "/memories/observability/hitl-policies.md",
            "/memories/observability/knowledge/",
        ]

    def get_interrupt_config(self) -> Dict[str, Any]:
        return {}

    def make_backend(self, runtime: Any) -> Any:
        from deepagents.backends import (
            CompositeBackend,
            FilesystemBackend,
            StateBackend,
            StoreBackend,
        )
        from k8s_autopilot.utils.memory import get_project_root

        root = get_project_root()
        default = FilesystemBackend(
            root_dir=str(root),
            virtual_mode=True,
        )

        return CompositeBackend(
            default=default,
            routes={
                "/memories/": StoreBackend(
                    runtime,
                    namespace=lambda ctx: (
                        ctx.context.get("org_name", "default_org")
                        if isinstance(ctx.context, dict)
                        else getattr(ctx.context, "org_name", "default_org"),
                    ),
                ),
                "/shared/": StoreBackend(
                    runtime,
                    namespace=lambda _ctx: ("shared",),
                ),
                "/skills/": StateBackend(runtime),
            },
        )

    def build_store(self) -> Any:
        store = InMemoryStore()
        project_root = get_project_root()
        memory_dir = project_root / "memory"

        namespace = ("default_org",)

        if memory_dir.exists():
            for path in memory_dir.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    key = path.relative_to(memory_dir).as_posix()
                    try:
                        store.put(
                            namespace,
                            key,
                            dict(create_file_data(path.read_text(encoding="utf-8"))),
                        )
                    except UnicodeDecodeError:
                        pass

        # Pre-seed operations-log if not populated to prevent "File not found" read errors
        if store.get(namespace, "observability/operations-log.md") is None:
            empty_log = (
                "# Observability Operations Journal\n\n"
                "Auto-generated log of operations performed in this session. "
                "Used by the coordinator to maintain context across "
                "conversation turns and after summarization.\n"
            )
            store.put(
                namespace,
                "observability/operations-log.md",
                dict(create_file_data(empty_log)),
            )

        return store

    def build_checkpointer(self) -> Any:
        """Return None to inherit the parent supervisor's checkpointer.

        Per-invocation mode (checkpointer=None) is the recommended pattern
        for subagents invoked as tools.  The child inherits the parent's
        checkpointer via the config passed to ainvoke(), enabling native
        interrupt()/resume support without manual bridging.

        Reference: LangGraph docs — Subgraph persistence / Per-invocation.
        """
        return None

    async def build_agent(self) -> Any:
        if getattr(self, "_agent", None):
            return self._agent

        logger.info("Building Observability deep agent graph")

        self._store = self.build_store()
        checkpointer = self.build_checkpointer()
        tools = await self.get_tools()
        subagents = await self.get_subagent_specs()
        middleware = build_obs_operator_middleware(
            config=self._config,
            model=self.get_model(),
            backend=self.make_backend,
        )

        self._agent = create_deep_agent(
            model=self.get_model(),
            name=self.name,
            system_prompt=self.system_prompt,
            tools=tools,
            subagents=subagents,
            skills=self.get_skill_paths(),
            memory=self.get_memory_paths(),
            backend=self.make_backend,
            store=self._store,
            checkpointer=checkpointer,
            interrupt_on=self.get_interrupt_config(),
            context_schema=self.context_schema,
            middleware=middleware,
        )
        return self._agent

    def seed_files(
        self,
        skills_dir: Optional[Any] = None,
        memory_dir: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return K8sBackendMixin.seed_files(
            skill_paths=self.get_skill_paths(),
            memory_paths=self.get_memory_paths(),
        )

    def input_transform(self, send_payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = send_payload.get("messages", [])
        files = self.seed_files()
        transformed: Dict[str, Any] = {
            "messages": messages,
        }
        if files:
            transformed["files"] = files
        return transformed

    def build_context(
        self,
        supervisor_state: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        state = supervisor_state or {}

        ctx: Dict[str, Any] = {
            "prometheus_url":       os.getenv("PROMETHEUS_BASE_URL", os.getenv("PROMETHEUS_URL", "")),
            "alertmanager_url":     os.getenv("ALERTMANAGER_BASE_URL", ""),
            "default_backend_id":   os.getenv("OBS_DEFAULT_BACKEND_ID", "default"),
            "cluster_context":      os.getenv("K8S_CONTEXT", ""),
            "kubeconfig_path":      os.getenv("KUBECONFIG", ""),
            "default_namespace":    os.getenv("K8S_DEFAULT_NAMESPACE", "default"),
        }

        if state.get("session_id"):
            ctx["session_id"] = state["session_id"]
        if state.get("task_id"):
            ctx["task_id"] = state["task_id"]

        # ── SRE investigation context ─────────────────────────────────
        # Propagate investigation-scoped fields from supervisor state so
        # subagents can auto-filter queries by service/env/incident.
        _sre_fields = (
            "service_name", "environment", "tenant_id",
            "time_window", "incident_id", "user_id",
        )
        for field in _sre_fields:
            val = state.get(field)
            if val:
                ctx[field] = val

        # Merge additional_labels if provided by supervisor
        extra_labels = state.get("additional_labels")
        if isinstance(extra_labels, dict) and extra_labels:
            ctx["additional_labels"] = extra_labels

        caller_ctx: Dict[str, Any] = state.get("context") or {}
        if isinstance(caller_ctx, dict):
            ctx.update({k: v for k, v in caller_ctx.items() if v is not None and v != ""})

        for key in ("prometheus_url", "alertmanager_url", "cluster_context", "kubeconfig_path"):
            if ctx.get(key) == "":
                ctx.pop(key, None)

        # ── Cross-domain context ──────────────────────────────────────
        # If the supervisor routed here after another coordinator deferred
        # with "outside my scope", inject the structured prior context so
        # the observability agent can use it instead of asking the user.
        cross_domain = state.get("cross_domain_context")
        if isinstance(cross_domain, dict) and cross_domain:
            ctx["cross_domain_context"] = cross_domain

        # Propagate accumulated domain summaries for the blackboard pattern
        domain_summaries = state.get("domain_summaries")
        if isinstance(domain_summaries, list) and domain_summaries:
            ctx["domain_summaries"] = domain_summaries

        return ctx

    def output_transform(
        self,
        agent_state: Dict[str, Any],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        state: Dict[str, Any] = agent_state
        if not isinstance(agent_state, dict) and hasattr(agent_state, "model_dump"):
            state = agent_state.model_dump()

        final_message: Optional[str] = None
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            final_message = getattr(last_msg, "content", None) or (
                last_msg.get("content") if isinstance(last_msg, dict) else None
            )

        output: Dict[str, Any] = {
            "final_message": final_message or "Observability operator completed.",
            "status": "completed",
            "observability_output": {
                "messages": messages,
                "structured_response": state.get("structured_response"),
            },
            # ── Domain summary for supervisor blackboard ──────────────
            # Compact structured summary that the supervisor accumulates
            # and passes to downstream coordinators for cross-domain
            # awareness.  Keeps only distilled findings, not raw data.
            "domain_summary": extract_domain_summary(
                domain="observability",
                final_message=final_message,
            ),
        }

        return output

# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_observability_coordinator(
    config: Optional["Config"] = None,
    mcp_server_filter: Optional[List[str]] = None,
) -> ObservabilityCoordinator:
    """
    Create an ObservabilityCoordinator instance.

    Usage::

        from k8s_autopilot.core.agents.observability.coordinator import create_observability_coordinator
        coordinator = create_observability_coordinator(config)
    """
    return ObservabilityCoordinator(config=config, mcp_server_filter=mcp_server_filter)
