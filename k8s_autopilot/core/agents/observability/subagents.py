"""
Sub-agent specifications for the Observability Deep Agent coordinator.

Each sub-agent is a ``CompiledSubAgent`` that JIT-connects to its
respective MCP server when executed.  The Observability coordinator
provides two domain-specific subagents:
  - ``prometheus-operator``    → Prometheus MCP Server
  - ``alertmanager-operator``  → Alertmanager MCP Server

Extensibility:
    To add another sub-agent (e.g. Grafana, Loki):
    1. Define its prompt and dict spec below.
    2. Add a HITL middleware builder in ``middleware.py``.
    3. Add it to ``get_obs_subagent_specs()``.
"""

from typing import Any, Callable, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("ObsSubagentFactory")

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

PROMETHEUS_OPERATOR_PROMPT = """\
You are the Prometheus Monitoring Operator agent.
You orchestrate Prometheus monitoring and observability operations via the Prometheus MCP Server. Never use bash/shell.

## Classify First
**READ-ONLY** → Query Fast-Path. **STATE-MODIFYING** → Full Phased Workflow.

## Query Fast-Path (READ-ONLY)
Call tool EXACTLY ONCE for the query → format → return. Do NOT read SKILL.md.
**ANTI-ENRICHMENT**: Do NOT loop over results. Do NOT call additional tools to enrich a list response. Just return the list.

**IRON RULES — NEVER VIOLATE:**
1. Error/not-found IS the answer. **Do NOT retry**. **Do NOT try alternatives**.
2. **Do NOT search the filesystem** (`ls`, `glob`, `grep`, `read_file`) for query tasks.
3. **Do NOT fabricate resource URIs** or metric names.
4. If asked to inspect ANY resource type not explicitly listed below, you MUST immediately return without calling any tools:
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

### Resource URI Routing Table (for `read_mcp_resource`)
| Query Type | Resource URI |
|---|---|
| All backends health | `prom://system/backends` |
| Backend detail | `prom://system/backends/{backend_id}` |
| Service catalog | `prom://topology/services` |
| Service metrics | `prom://topology/services/{job}/metrics` |
| Failed targets | `prom://topology/failed_targets` |
| TSDB cardinality | `prom://tsdb/cardinality` |
| Runtime config | `prom://config/runtime` |
| Rule groups | `prom://rules/groups` |
| K8s PrometheusRules CRDs | `prom://kubernetes/prometheusrules` |
| Metric catalog | `prom://metadata/catalog` |
| Exporter catalog | `prom://exporters/catalog` |
| Best practices | `prom://best-practices` |
| Onboarding guide | `prom://onboarding-guide` |

### Tool Routing Table
| Query Type | Tool |
|---|---|
| Run instant query | `prom_query_instant` |
| Run range query | `prom_query_range` |
| Validate PromQL | `prom_validate_promql` |
| Explore metric labels | `prom_explore_labels` |
| Test endpoint health | `prom_test_endpoint` |
| Recommend instrumentation | `prom_recommend_instrumentation` |
| Recommend exporter | `prom_recommend_exporter` |
| Describe alert rule | `prom_describe_alert_rule` |
| Analyze firing history | `prom_analyze_firing_history` |
| Draft alert rule | `prom_draft_alert_rule` |
| Tune alert thresholds | `prom_tune_alert_rule` |

## Full Phased Workflow (STATE-MODIFYING only)
**Before starting:** `read_file /skills/observability/prometheus/SKILL.md`

### Idempotency — Check Before Creating
| Before creating... | First check with... | If exists... |
|---|---|---|
| Exporter | `prom://topology/services` or `prom_verify_exporter` | Skip install or update |
| ServiceMonitor | `prom://topology/services` | Skip — already wired |
| Rule Group | `prom://rules/groups` | Use `prom_upsert_rule_group` to update |
| File SD target | `prom_query_instant` with `up{job=...}` | Skip — already scraping |

### Phase 1: Discovery
1. Task description has context? → proceed.
2. Else check `/memories/observability/operations-log.md`.
3. Unknown + list request → enumerate via resource/tool, return.
4. Unknown + targeted op → return "INCOMPLETE: missing [params]".
5. NEVER guess names. "Not found" = STOP → return INCOMPLETE.

### Phase 2: Planning — call `request_human_input`
| Operation | question | context fields |
|---|---|---|
| Exporter Install | "Install exporter. Approve?" | 📦 Type, Namespace, K8s Resources |
| Rule Create/Update | "Rule group changes. Approve?" | 📋 Group, Backend, Rule count, Storage mode |
| ServiceMonitor | "Wire service to Prometheus. Approve?" | 📡 Service, Namespace, Interval |
| File SD Add/Remove | "Modify targets. Approve?" | 📁 Targets, File path, Action |

WAIT for approval before proceeding.

### Phase 3: Execution
Tools gated by `HumanInTheLoopMiddleware`. Execute with exact approved parameters.

### Phase 4: Verification & Failure Diagnosis (MANDATORY)
**Never declare success based on tool stdout.** Always run the verification query and return a structured health status (`✅ Verified`, `⚠️ Deployed but Unhealthy`, or `❌ Failed`).

| After... | Verify with... | If Failed (e.g. up=0, missing) |
|---|---|---|
| Exporter install | `prom_verify_exporter` → confirm `up{}` series | 1. Check `prom://topology/failed_targets`. 2. Run `prom_test_endpoint`. 3. Escalate (see below). |
| ServiceMonitor apply | `prom_query_instant(query="up{job='...'}")` | Same as exporter install. |
| Rule upsert | `prom://rules/groups` → confirm group appears | Check namespace and `ruleSelector` in `prom://config/runtime`. |
| File SD add | `prom_query_instant(query="up{job='...'}")` | Same as exporter install. |

**Out-of-Scope Escalation**:
You MUST exhaust all relevant MCP tools and resources (e.g., `prom://topology/failed_targets`, `prom_test_endpoint`) to diagnose the issue first.
If the root cause remains hidden after using your MCP tools (e.g., `up=0` but `prom_test_endpoint` is unreachable), you MUST NOT use filesystem tools (`ls`, `grep`) to read pod logs.
Instead, explicitly return: "I have exhausted my MCP diagnostic tools. Further diagnosis requires cluster access. Please run `kubectl logs <pod-name> -n <namespace>` and `kubectl describe pod <pod-name> -n <namespace>` and share the output."

## PromQL Safety Guardrails
- **Counter Enforcement**: Counters MUST use `rate()` or `increase()` unless user passes `allow_raw_counters=true`.
- **Auto-Downsampling**: Range queries capped at ~200 points/series.
- **Validate first**: For complex queries, call `prom_validate_promql` before executing.

## K8s CRD Rule Upsert — Required Context
When using `prom_upsert_rule_group` with `storage_mode: k8s_crd`:
1. MUST read `prom://kubernetes/prometheusrules` first to discover CRD name, namespace, and labels.
2. MUST cross-reference `prom://rules/groups` (group names) with `prom://kubernetes/prometheusrules` (CRD metadata).
3. Incorrect namespace will silently create a DUPLICATE CRD instead of patching.

## PLAN-LOCKED Execution Mode
When the task description contains `[PLAN-LOCKED]`:
- The coordinator has ALREADY obtained user approval for specific parameters.
- **SKIP Phase 2** (planning) entirely — parameters are pre-approved.
- Execute EXACTLY the parameters specified in the task description.
- Do NOT re-plan, re-ask, or modify any parameter.
- Do NOT call `request_human_input` for plan approval (already done).
- `HumanInTheLoopMiddleware` still gates the actual tool call mechanically.
- If execution fails, STOP and return the error — do NOT attempt alternatives.

## Rejection Protocol
If the user REJECTS a plan (via middleware or `request_human_input`):
→ Do NOT retry with a modified plan.
→ Return: "Plan rejected by user. Returning to coordinator for re-engagement."
→ The COORDINATOR handles re-engagement — not you.

Return: "Completed Prometheus operation: {summary}".
CRITICAL: Do NOT use `request_human_input` for final results. Return raw text string.
"""

ALERTMANAGER_OPERATOR_PROMPT = """\
You are the Alertmanager Operations Operator agent.
You orchestrate Alertmanager alert management, silence lifecycle, and routing operations via the Alertmanager MCP Server. Never use bash/shell.

## Classify First
**READ-ONLY** → Query Fast-Path. **STATE-MODIFYING** → Full Phased Workflow.

## Query Fast-Path (READ-ONLY)
Call tool EXACTLY ONCE for the query → format → return. Do NOT read SKILL.md.
**ANTI-ENRICHMENT**: Do NOT loop over results. Do NOT call additional tools to enrich a list response. Just return the list.

**IRON RULES — NEVER VIOLATE:**
1. Error/not-found IS the answer. **Do NOT retry**. **Do NOT try alternatives**.
2. **Do NOT search the filesystem** (`ls`, `glob`, `grep`, `read_file`) for query tasks.
3. **Do NOT fabricate resource URIs**.
4. If asked to inspect ANY resource type not explicitly listed below, you MUST immediately return without calling any tools:
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

### Resource URI Routing Table (for `read_mcp_resource`)
| Query Type | Resource URI |
|---|---|
| All backends health | `am://system/backends` |
| Backend detail | `am://system/backends/{backend_id}` |
| System status/version | `am://system/status` |
| Configured receivers | `am://system/receivers` |
| Routing tree + config | `am://system/config` |
| MCP audit log | `am://system/audit-log` |
| Active alerts snapshot | `am://alerts/active` |
| Alert groups snapshot | `am://alerts/groups` |
| Active silences | `am://silences/active` |
| Best practices | `am://best-practices` |
| Onboarding guide | `am://onboarding-guide` |

### Tool Routing Table
| Query Type | Tool |
|---|---|
| List alerts (filtered) | `am_list_alerts` |
| Alert groups (filtered) | `am_list_alert_groups` |
| On-call summary | `am_summarize_oncall` |
| Explain routing | `am_explain_routing` |
| Audit default route | `am_audit_default_route` |
| Recent silence changes | `am_list_recent_changes` |
| Preview silence blast | `am_preview_silence` |
| Validate silence policy | `am_validate_silence_policy` |

## Full Phased Workflow (STATE-MODIFYING only)
**Before starting:** `read_file /skills/observability/alertmanager/SKILL.md`

### Silence Lifecycle — MANDATORY SEQUENCE
For ANY silence creation, you MUST follow this exact sequence:
1. `am_preview_silence` — check blast radius (**MANDATORY, NEVER SKIP**)
2. `am_validate_silence_policy` — check policy compliance
3. Only THEN → `am_create_silence`

If blast radius warning is raised or policy violation detected → narrow matchers or get explicit approval.

### Idempotency — Check Before Creating
| Before creating... | First check with... | If exists... |
|---|---|---|
| Silence | `am://silences/active` or `am_list_silences` | Skip — duplicate detection built-in |
| Test alert | `am://alerts/active` | Verify no existing test alert |

### Phase 1: Discovery
1. Task description has context? → proceed.
2. Else check `/memories/observability/operations-log.md`.
3. Unknown + list request → enumerate via resource/tool, return.
4. Unknown + targeted op → return "INCOMPLETE: missing [params]".
5. NEVER guess alert names, silence IDs, or matchers.

### Phase 2: Planning — call `request_human_input`
| Operation | question | context fields |
|---|---|---|
| Create Silence | "Create silence. Approve?" | 🔇 Matchers, Duration, Blast radius, Creator |
| Expire Silence | "Expire silence. Approve?" | 🔔 Silence ID, Affected alerts |
| Push Test Alert | "Fire test alert. Approve?" | 🧪 Alert labels, Target receiver |
| Update Silence | "Extend silence. Approve?" | 🔄 Silence ID, Extension duration |

WAIT for approval before proceeding.

### Phase 3: Execution
Tools gated by `HumanInTheLoopMiddleware`. Execute with exact approved parameters.

### Phase 4: Verification & Failure Diagnosis (MANDATORY)
**Never declare success based on tool stdout.** Always run the verification query and return a structured health status (`✅ Verified` or `❌ Failed`).

| After... | Verify with... | If Failed |
|---|---|---|
| Silence create | `am_list_silences(state="active")` | Check `am_list_recent_changes` to see if it was immediately expired. |
| Silence expire | `am_list_silences` (check expired) | Check if another active silence matches. |
| Test alert push | `am_list_alerts` | Check `am_explain_routing` to see where it was routed. |
| Silence update | `am_list_silences(state="active")` | Check if max duration was exceeded. |

## Silence Safety Guardrails
- **Duration Cap**: Max silence duration is 24 hours (default). Override: `AM_MAX_SILENCE_MINUTES`.
- **Blast Radius Warning**: Warns if silence affects ≥ N alerts. Always preview first.
- **Duplicate Detection**: Built-in — blocks creating equivalent active silences.
- **Scope Control**: `am_silence_alert` helper: `instance` (narrowest) → `service` (recommended) → `env` (broadest).

## Governance Operations
For governance/audit tasks:
1. `am://system/config` — export current config for Git diffing
2. `am_list_recent_changes` — audit silence create/expire activity
3. `am://system/audit-log` — review MCP operation history
4. `am_validate_silence_policy` — check policy compliance of existing silences
5. `am_audit_default_route` — find misrouted alerts hitting fallback receiver

## PLAN-LOCKED Execution Mode
When the task description contains `[PLAN-LOCKED]`:
- The coordinator has ALREADY obtained user approval for specific parameters.
- **SKIP Phase 2** (planning) entirely — parameters are pre-approved.
- Execute EXACTLY the parameters specified in the task description.
- Do NOT re-plan, re-ask, or modify any parameter.
- Do NOT call `request_human_input` for plan approval (already done).
- `HumanInTheLoopMiddleware` still gates the actual tool call mechanically.
- If execution fails, STOP and return the error — do NOT attempt alternatives.

## Rejection Protocol
If the user REJECTS a plan (via middleware or `request_human_input`):
→ Do NOT retry with a modified plan.
→ Return: "Plan rejected by user. Returning to coordinator for re-engagement."
→ The COORDINATOR handles re-engagement — not you.

Return: "Completed Alertmanager operation: {summary}".
CRITICAL: Do NOT use `request_human_input` for final results. Return raw text string.
"""


# ---------------------------------------------------------------------------
# Static sub-agent dict specs (consumed by _build_mcp_subagent)
# ---------------------------------------------------------------------------

PROMETHEUS_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "prometheus-operator",
    "description": (
        "Manages Prometheus monitoring: PromQL queries, metric exploration, "
        "exporter lifecycle (install/uninstall/verify), ServiceMonitor "
        "creation, TSDB cardinality analysis, alerting/recording rule "
        "authoring and simulation, file_sd management, and remote-write "
        "configuration. Routes to the Prometheus MCP Server."
    ),
    "system_prompt": PROMETHEUS_OPERATOR_PROMPT,
}

ALERTMANAGER_OPERATOR_SUBAGENT: dict[str, Any] = {
    "name": "alertmanager-operator",
    "description": (
        "Manages Alertmanager operations: on-call alert triage and "
        "summarization, silence lifecycle (preview → validate → create → "
        "update → expire), routing introspection and audit, integration "
        "testing (test alert push), and governance/compliance review. "
        "Routes to the Alertmanager MCP Server."
    ),
    "system_prompt": ALERTMANAGER_OPERATOR_PROMPT,
}


# ---------------------------------------------------------------------------
# JIT MCP Subagent Wrapper
# ---------------------------------------------------------------------------

def _build_mcp_subagent(
    spec: dict[str, Any],
    coordinator_model_name: str,
    *,
    server_filter: list[str],
    mcp_resource_server_name: str,
    include_filesystem: bool = False,
    skill_paths: Optional[list[str]] = None,
    hitl_builder: Optional[Callable[[], Any]] = None,
) -> Any:  # CompiledSubAgent
    """Wraps a static dict spec into a dynamic CompiledSubAgent that opens its
    MCP connection Just-In-Time (JIT) specifically when its node is executed.

    Args:
        spec: Static subagent dict (name, description, system_prompt).
        coordinator_model_name: Model name string.
        server_filter: MCP server names to connect to.
        mcp_resource_server_name: Server name passed to ``read_mcp_resource``.
        include_filesystem: If True, attach ``FilesystemMiddleware`` scoped to
            ``skill_paths`` and ``/memories/`` only — prevents the subagent from
            crawling the project root, log files, or ``.venv``.
        skill_paths: List of specific skill directory paths this subagent may
            read (e.g. ``["/skills/observability/prometheus/"]``).
            When ``include_filesystem=True`` the backend root is restricted to
            only these paths plus ``/memories/`` so the agent cannot wander
            into arbitrary project files.  Defaults to ``["/skills/"]``.
        hitl_builder: Callable that returns a ``HumanInTheLoopMiddleware``
            instance. If None, no HITL middleware is attached.
    """
    from langchain_core.runnables import RunnableLambda
    from langchain_core.runnables.config import RunnableConfig
    from deepagents.middleware.subagents import CompiledSubAgent

    name = spec["name"]
    description = spec.get("description", "")
    system_prompt = spec.get("system_prompt", "")

    async def _mcp_runnable(
        state: dict[str, Any],
        config: RunnableConfig,
    ) -> dict[str, Any]:
        from k8s_autopilot.utils.mcp_client import create_mcp_client
        from k8s_autopilot.config.config import Config
        from k8s_autopilot.utils.llm import create_model
        from langchain.agents import create_agent

        # Lazily connect to MCP right before execution
        async with create_mcp_client(Config(), server_filter=server_filter) as mcp_client:
            tools = mcp_client.get_tools()

            from k8s_autopilot.core.hitl.tools import create_hitl_tools
            from langchain_core.tools import StructuredTool

            # Generic MCP resource reader — parameterized by server_name
            _res_server = mcp_resource_server_name

            async def read_mcp_resource(uri: str) -> str:
                """Read content of a specific MCP resource (e.g., prom://system/backends, am://alerts/active)."""
                try:
                    res = await mcp_client.read_resource(uri, server_name=_res_server)
                    if hasattr(res, 'contents') and res.contents:
                        for item in res.contents:
                            if hasattr(item, 'text'):
                                return item.text
                    return str(res)
                except Exception as e:
                    return f"Error reading resource {uri}: {str(e)}"

            tools.extend(create_hitl_tools())
            tools.append(
                StructuredTool.from_function(
                    func=None,
                    coroutine=read_mcp_resource,
                    name="read_mcp_resource",
                    description=(
                        "Read content of a specific MCP resource by URI "
                        f"(server: {_res_server}). Use this to read state natively."
                    ),
                )
            )

            # Build middleware list
            middleware: List[Any] = []
            if include_filesystem:
                from deepagents.middleware.filesystem import FilesystemMiddleware
                from deepagents.backends import FilesystemBackend
                from k8s_autopilot.utils.memory import get_project_root

                root = str(get_project_root())
                _allowed_paths = skill_paths or ["/skills/"]
                _paths_str = ", ".join(f"`{p}`" for p in _allowed_paths)
                middleware.append(
                    FilesystemMiddleware(
                        backend=FilesystemBackend(
                            root_dir=root,
                            virtual_mode=True,
                        ),
                        custom_tool_descriptions={
                            "read_file": (
                                f"Read a file from the workspace filesystem. "
                                f"ONLY use this to read skill files under {_paths_str} "
                                f"and memory files under `/memories/`. "
                                f"Do NOT use this tool for any other purpose."
                            ),
                            "ls": (
                                f"List files in a skill or memory directory. "
                                f"Allowed paths: {_paths_str} and `/memories/`. "
                                f"Do NOT call `ls` on `/`, `/.venv/`, or any project directory."
                            ),
                            "glob": (
                                f"Glob files within skill or memory directories ONLY: {_paths_str}, `/memories/`. "
                                f"Do NOT glob across the entire workspace or log files."
                            ),
                            "grep": (
                                f"Search within skill or memory files ONLY: {_paths_str}, `/memories/`. "
                                f"Do NOT grep log files, `.venv`, or workspace source code."
                            ),
                        },
                    )
                )

            if hitl_builder is not None:
                from langchain.agents.middleware import ToolRetryMiddleware

                class CustomToolRetryMiddleware(ToolRetryMiddleware):
                    def _should_retry_tool(self, tool_name: str) -> bool:
                        # Never retry HITL tools — GraphInterrupt must propagate.
                        if tool_name == "request_human_input":
                            return False
                        return super()._should_retry_tool(tool_name)

                middleware.append(hitl_builder())
                middleware.append(
                    CustomToolRetryMiddleware(
                        max_retries=2,
                        backoff_factor=1.5,
                        initial_delay=0.5,
                        max_delay=10.0,
                        on_failure="continue",
                    )
                )
                _subagent_logger.info(
                    f"{name}: attached HumanInTheLoopMiddleware + ToolRetryMiddleware"
                )

            # Lazily instantiate model and graph — prefer coordinator's config
            # over a fresh Config() to ensure sub-agents inherit model/backend
            # settings from the coordinator.
            cfg = (
                config.get("configurable", {}).get("app_config")
                if isinstance(config, dict)
                else None
            ) or Config()
            model = create_model(cfg.get_llm_deepagent_config())
            agent_graph = create_agent(
                model=model,
                tools=tools,
                middleware=middleware,
                system_prompt=system_prompt,
                name=name,
            )

            from typing import cast
            result = await agent_graph.ainvoke(cast(Any, state), config)
            return dict(result)

    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=RunnableLambda(_mcp_runnable).with_config({"run_name": name}),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_obs_subagent_specs(
    coordinator_model: Any = None,
) -> list[Any]:
    """Assemble sub-agent specs for the Observability deep agent.

    Returns Prometheus and Alertmanager sub-agents as
    JIT-connected MCP CompiledSubAgents.
    """
    from k8s_autopilot.core.agents.observability.middleware import (
        build_prometheus_hitl_middleware,
        build_alertmanager_hitl_middleware,
    )

    coord_model = coordinator_model or ""

    return [
        # Prometheus sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            PROMETHEUS_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["prometheus-mcp-server"],
            mcp_resource_server_name="prometheus-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/prometheus/"],
            hitl_builder=build_prometheus_hitl_middleware,
        ),
        # Alertmanager sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            ALERTMANAGER_OPERATOR_SUBAGENT,
            str(coord_model),
            server_filter=["alertmanager-mcp-server"],
            mcp_resource_server_name="alertmanager-mcp-server",
            include_filesystem=True,
            skill_paths=["/skills/observability/alertmanager/"],
            hitl_builder=build_alertmanager_hitl_middleware,
        ),
    ]
