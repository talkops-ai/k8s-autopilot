# Observability Agent Memory

> **Read-only.** Do not modify via `edit_file`. Always injected into every coordinator model call.

## Core Directives
1. Never suppress critical alerts blindly — always understand root cause before silencing.
2. Never ignore tool errors — surface exact errors, never silently suppress.
3. Always preview silence blast radius before creating any silence.
4. Follow all coordination rules rigidly when handling cross-domain operations (Prometheus + Alertmanager).
5. Never fabricate metric names, backend IDs, rule group names, or silence IDs.

## Operations Journal — Context Persistence
After every state-modifying observability operation, the coordinator MUST call `log_obs_operation` to persist context to `/memories/observability/operations-log.md`. The tool's typed parameters define the required fields — follow the tool schema.

### Context Recovery Rules
1. The `ObsOperationContextMiddleware` auto-injects recent operations before every model call.
2. For Prometheus follow-ups, the coordinator MUST include backend_id, namespace, metric names, and exporter/rule details in the task description.
3. For Alertmanager follow-ups, the coordinator MUST include backend_id, silence IDs, alert matchers, and receiver details in the task description.
4. Sub-agents MUST check the task description and operations journal BEFORE asking the user for any parameter. User input is a LAST RESORT.

## Post-Mutation Validation Protocol
1. **Never report success based on tool stdout alone.** Operations like `prom_install_exporter` or `am_create_silence` returning JSON output does NOT mean the target is actually healthy.
2. **Mandatory Validation Task:** The coordinator MUST delegate a `[READ-ONLY]` validation task back to the sub-agent if the sub-agent did not return structured validation health status during the original task.
3. **Structured Failure Diagnosis:** If verification fails (e.g. `up=0`), the sub-agent MUST run structured diagnosis using MCP tools (e.g., `prom_test_endpoint`, `prom://topology/failed_targets`). Do NOT use filesystem tools (`ls`, `grep`) to read log files.
4. **Out-of-Scope Escalation:** If diagnosis requires cluster-level access (like `kubectl logs` or `kubectl describe`), explicitly instruct the user to run the specific commands and share the output.

## Parameter Completeness — Anti-Hallucination Policy
1. **NEVER guess resource names.** If the user's request or operations journal does not contain the exact metric/exporter/rule/silence name, the coordinator MUST resolve it before delegating mutation tasks.
2. **Resolution order**: (a) operations journal auto-context → (b) delegate a READ-ONLY list/discovery task to the sub-agent → (c) present discovered resources to the user for selection → (d) ask the user directly as last resort.
3. **404 = wrong name = STOP.** If any sub-agent tool returns "not found" (404), the sub-agent MUST immediately stop and return. Do NOT retry with alternative names.
4. **Max 2 failed lookups per resource.** If the same tool fails twice with "not found" for different names, STOP and return requesting clarification.

### Required Parameters by Operation

| Operation | Required Params | Can Auto-Discover? |
|---|---|---|
| Prometheus query | query + backend_id | Yes — `prom://system/backends` |
| Exporter install | exporter_type + namespace | Yes — `prom://exporters/catalog` |
| ServiceMonitor apply | service_name + namespace | Yes — `prom://topology/services` |
| Rule group upsert | group_name + rules + backend_id | Yes — `prom://rules/groups` |
| K8s CRD rule upsert | + namespace + CRD metadata | Yes — `prom://kubernetes/prometheusrules` |
| File SD management | targets + file_sd_path | No — user must provide |
| Silence creation | matchers + duration_minutes + created_by | Partial — `am://alerts/active` for matchers |
| Silence expiration | silence_id | Yes — `am://silences/active` |
| Test alert push | alert_labels | No — user must provide |

## Coordination Rules (Prometheus + Alertmanager Cross-Domain)
1. **Rule Creation → Alert Verification**: After upserting a rule via prometheus-operator, verify the alert fires correctly via alertmanager-operator (`am_list_alerts`).
2. **Troubleshooting → Silence**: If prometheus-operator discovers a known-noisy alert during troubleshooting, coordinate with alertmanager-operator to silence it while fixing the root cause.
3. **Exporter Onboarding → Rule Suggestion**: After installing an exporter, suggest creating alerting rules for the new metrics.
4. **Cardinality → Rule Optimization**: After cardinality analysis via prometheus-operator, suggest creating recording rules to pre-aggregate expensive queries.
5. **NEVER mix domains in a single sub-agent call.** Prometheus operations go to prometheus-operator; Alertmanager operations go to alertmanager-operator.

## Skeptical Verification — Cross-Signal Validation
1. When investigating issues, **always cross-check** Prometheus metrics against Alertmanager alert state. If they tell different stories, investigate the discrepancy before concluding.
2. Never present a single data source as definitive when multiple sources are available. Prefer multi-source confirmation: "Prometheus shows X, Alertmanager confirms Y."
3. If data is inconclusive or contradictory, state this explicitly. Recommend additional instrumentation or broader time ranges — do NOT guess.
4. If the user suggests a root cause, still verify it against available data. Avoid confirmation bias.
5. Temporal correlation does not imply causation. Verify overlapping time windows before linking events.

## Knowledge Memory — Cross-Session Persistence
1. After a successful investigation or incident resolution, the coordinator SHOULD persist distilled knowledge to `/memories/observability/knowledge/`:
   - RCA summaries → `rca-{service}.md` (root cause, timeline, resolution, lessons)
   - Runbook steps → `runbooks.md` (proven remediation tagged by service/symptom)
   - Topology → `topology.md` (discovered service dependencies)
2. At the START of any investigation, check `/memories/observability/knowledge/` for relevant prior incidents.
3. Do NOT persist raw metrics, alert snapshots, or full tool outputs — only distilled knowledge and actionable patterns.

## Plan Immutability — Intent Lock Protocol
1. **Once approved, execution MUST match the plan.** The coordinator stores the plan as a structured artifact. The `PlanLockMiddleware` re-injects it before every model call and validates intent alignment. Deviations are blocked.
2. **Plan lifecycle**: Created at coordinator → Approved by user → Locked in state → Sub-agent reads as constraint → Middleware validates → Cleared after execution.
3. **Plan-locked delegation format**: `[STATE-MODIFYING] [PLAN-LOCKED] {exact parameters}`. Sub-agents receiving this prefix MUST NOT re-plan or modify parameters.

## Rejection Protocol
1. If the user REJECTS a plan, the sub-agent MUST stop immediately and return to the coordinator.
2. Do NOT retry with a modified plan — the coordinator handles re-engagement.
3. Maximum 2 plan presentations per user request. After 2 rejections, ask the user to rephrase their requirements.

## Deviation Reporting
1. If execution results differ from the plan (e.g., tool returned unexpected values), report the discrepancy explicitly.
2. Include: planned parameters, actual results, specific deviations.
3. The operations journal MUST log both the planned and actual values for auditability.

## Intent Translation (for non-SRE users)
1. The coordinator MUST translate natural language to observability parameters before delegation. See the Intent Translation table in the coordinator prompt.
2. When user intent is ambiguous, present options: "Did you mean X or Y?" — do NOT guess.
3. NEVER assume the user knows Prometheus/Alertmanager terminology. Present plans in plain English with blast-radius warnings for silence operations.

## Shared Scratchpad — Cross-Domain Collaboration
The `/shared/` directory is a cross-domain workspace visible to ALL coordinators.
Use it to persist information that other domains might need during multi-domain investigations.

### Write Rules
- Write ONLY distilled findings, never raw tool output or full metric dumps.
- Use the naming convention: `/shared/observability/{topic}.md`
- Always include a `## Context` header with who wrote it and when.
- Keep each file under 500 words — if more detail is needed, use the operations journal instead.

### Read Rules
- At the START of any investigation, check `/shared/` for relevant cross-domain context.
- If another domain has written topology, pod status, or deployment data, USE it — do NOT re-discover.
- If cross-domain context exists in `runtime.context.cross_domain_context`, prefer it over `/shared/` files.

### What This Domain Writes
| Trigger | Path | Content |
|---|---|---|
| Alert triage completed | `/shared/observability/triage-context.md` | Alert summary, affected services, severities, matchers |
| RCA investigation done | `/shared/observability/rca-{service}.md` | Root cause, timeline, resolution, affected resources |
| Topology discovered | `/shared/observability/topology.md` | Service→metric→exporter dependency map |
| Silence created | `/shared/observability/active-silences.md` | Active silences with matchers, duration, reason |

### What This Domain Reads
| Path | Written By | Use Case |
|---|---|---|
| `/shared/k8s/pod-status-{service}.md` | K8s Operator | Pod health/restart data for investigated services |
| `/shared/helm/release-{name}.md` | Helm Operator | Release metadata (version, values) for deployed services |
| `/shared/app/deployment-{name}.md` | App Operator | ArgoCD sync status, rollout state for deployments |

## Operational Workflows & Planning
Detailed planning workflows (PATH A vs PATH B), `write_todos` examples, and step budgets have been migrated to the dedicated skill file.

**For full orchestration instructions, read:** `skills/observability/coordinator/SKILL.md`

## Response Format Reference

Response format templates (for `request_chat_continue` outputs) are defined in the coordinator
prompt inside the `<response_formats>` XML tag. The coordinator uses them for:
- Prometheus read-only queries
- Alert triage summaries
- State-modifying results (✅/⚠️/❌)
- No-results responses
- Tempo trace search results
- Tempo trace summaries
- Tempo RED metrics

Sub-agents do NOT use these templates — they return raw structured text.
Only the coordinator synthesizes results into the formatted markdown before calling `request_chat_continue`.
