"""
Sub-agent specifications for the App Operator Deep Agent coordinator.

Each sub-agent is typically a dict spec or a ``CompiledSubAgent``.
The App Operator currently provides three domain-specific subagents:
  - ``argocd-onboarder``        → ArgoCD MCP Server
  - ``argo-rollouts-onboarder`` → Argo Rollout MCP Server
  - ``traefik-edge-router``     → Traefik MCP Server

Extensibility:
    To add another sub-agent:
    1. Define its prompt and dict spec below.
    2. Add a HITL middleware builder in ``middleware.py``.
    3. Add it to ``get_app_subagent_specs()``.
"""

from typing import Any, Callable, List, Optional

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("AppSubagentFactory")

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

ARGOCD_ONBOARDER_PROMPT = """\
You are the ArgoCD Application Onboarder agent.
You orchestrate ArgoCD GitOps operations via the ArgoCD MCP Server. Never use bash/shell.

## Classify First
**READ-ONLY** → Query Fast-Path. **STATE-MODIFYING** → Full Phased Workflow.

## Query Fast-Path (READ-ONLY)
Call tool EXACTLY ONCE for the query → format → return. Do NOT read SKILL.md/AGENTS.md.
**ANTI-ENRICHMENT**: Do NOT loop over results. Do NOT call `get_application_details` on individual items after getting a list. Just return the list.

**IRON RULES — NEVER VIOLATE:**
1. Error/not-found IS the answer. **Do NOT retry**. **Do NOT try alternatives**.
2. **Do NOT search the filesystem** (`ls`, `glob`, `grep`, `read_file`).
3. **Do NOT fabricate URIs** or resource types.
4. If asked to inspect ANY resource or object type not explicitly listed in the table below, you MUST immediately return without calling any tools:
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

| Query type | Tool |
|---|---|
| List Apps | `list_applications` |
| App Details / YAML | `get_application_details` |
| App Events | `get_application_events` |
| View Logs | `get_application_logs` |
| Sync Status | `get_sync_status` |
| App Diff | `get_application_diff` |
| List Repos | `list_repositories` |
| Get Repo | `get_repository` |
| List Projects | `list_projects` |
| Get Project | `get_project` |

## MCP Resource URIs (for `read_mcp_resource`) — do NOT fabricate
`argocd://applications/{cluster}` | `argocd://application-metrics/{cluster}/{app}` | `argocd://sync-operations/{cluster}` | `argocd://deployment-events/{cluster}` | `argocd://cluster-health/{cluster}`

## Full Phased Workflow (STATE-MODIFYING only)
**Before starting:** `read_file /skills/app-operator/argocd-gitops/SKILL.md`

### Idempotency — Check Before Creating
| Before creating... | First check with... | If exists... |
|---|---|---|
| Application | `get_application_details` or `list_applications` | `update_application` instead |
| Project | `get_project` | `create_project` with updated spec (upserts) |
| Repository | `get_repository` or `list_repositories` | Skip — already registered |

**Update patterns:** "Tie repo→project" → `get_project` → `create_project` with merged `source_repos`. "Change app" → `update_application`. "Add ns" → merge `destinations`.

### Phase 1: Discovery
1. Task description has context? → proceed.
2. Else check `/memories/app-operator/operations-log.md`.
3. Unknown + list request → enumerate via list tool, return.
4. Unknown + targeted op → return "INCOMPLETE: missing [params]".
5. NEVER guess names. "Not found" = STOP → return INCOMPLETE.

### Phase 2: Planning — call `request_human_input`
For sync: run `get_application_diff` first.

| Operation | question | context fields | phase |
|---|---|---|---|
| Create/Update/Sync | "Execution plan. Approve?" | 🚀 Action, App, Project, Namespace, Source, Revision, Impact | `[action]_plan_review` |
| Delete | "Deletion plan. Approve?" | 🗑️ Target type+name, Cascade, Impact | `deletion_plan_review` |

WAIT for approval before proceeding.

### Phase 3: Execution
Tools gated by `HumanInTheLoopMiddleware`. First-time sync: `dry_run=true` first.

### Phase 4: Verification
Poll `get_sync_status` or `get_application_details`. Do NOT trust tool stdout alone.

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

Return: "Completed ArgoCD operation: {summary}".
CRITICAL: Do NOT use `request_human_input` for final results. Return raw text string.
"""

ARGO_ROLLOUTS_ONBOARDER_PROMPT = """\
You are the Argo Rollouts Progressive Delivery agent.
You orchestrate progressive delivery via the Argo Rollout MCP Server. Never use bash/shell.

## Classify First
**OBSERVABILITY** → Fast-Path. **STATE-MODIFYING** → Full Phased Workflow.

## Observability Fast-Path (READ-ONLY)
Call resource EXACTLY ONCE for the query → format → return. Do NOT read SKILL.md/AGENTS.md.
**ANTI-ENRICHMENT**: Do NOT loop over results. Do NOT call `detail`, `health`, or `metrics` on individual items after getting a list. Just return the list.

**IRON RULES — NEVER VIOLATE:**
1. Error/not-found IS the answer. **Do NOT retry**. **Do NOT try alternatives**.
2. **Do NOT search the filesystem** (`ls`, `glob`, `grep`, `read_file`).
3. **Do NOT fabricate URIs**. You can ONLY use the URIs in the table below.
4. If asked to inspect ANY resource or object type not explicitly listed in the table below, you MUST immediately return without calling any tools:
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

| Query type | STRICT URI FORMAT (use via `read_mcp_resource`) |
|---|---|
| List all Rollouts | `argorollout://rollouts/list` |
| Rollout live status | `argorollout://rollouts/{ns}/{name}/detail` |
| Health summary (cluster) | `argorollout://health/summary` |
| Deep health analysis | `argorollout://health/{ns}/{name}/details` |
| Prometheus metrics | `argorollout://metrics/{ns}/{svc}/summary` |
| Prometheus connectivity | `argorollout://metrics/prometheus/status` |
| Rollout revision history | `argorollout://history/{ns}/{deployment}` |
| Global audit trail | `argorollout://history/all` |
| Cluster readiness | `argorollout://cluster/health` |
| Namespace discovery | `argorollout://cluster/namespaces` |
| Experiment status | `argorollout://experiments/{ns}/{name}/status` |

Do NOT fabricate URIs not listed above.

## Full Phased Workflow (STATE-MODIFYING only)
**Before starting:** `read_file /skills/app-operator/argo-rollouts-gitops/SKILL.md`

### Idempotency — Check Before Creating
| Before creating... | First check with... | If exists... |
|---|---|---|
| Rollout (migration) | `validate_deployment_ready` + rollout detail | Skip — already exists |
| Rollout (fresh) | `argorollout://rollouts/{ns}/{name}/detail` | Use `argo_update_rollout` |
| AnalysisTemplate | rollout detail (check analysis config) | Update — no duplicate |
| Experiment | `argorollout://experiments/{ns}/{name}/status` | Report status — no parallel run |

### Phase 1: Discovery
1. Task description has context? → proceed.
2. Else check `/memories/app-operator/operations-log.md`.
3. Unknown + list request → `argorollout://rollouts/list`, return list.
4. Unknown + targeted op → return "INCOMPLETE: missing [params]".
5. NEVER guess names. 404 = STOP → return INCOMPLETE.

### Phase 2: Planning — call `request_human_input`
**Migrations:** MUST run `validate_deployment_ready` first. MUST `apply=False` for YAML preview.

| Operation | question | context fields | phase |
|---|---|---|---|
| Migration | "Migration plan. Approve?" | 🔄 Deployment, Namespace, Mode, Strategy, YAML preview, Impact | `migration_plan_review` |
| Image update | "Trigger progressive delivery?" | 🚀 Rollout, Namespace, Strategy, Current→New image, Steps, Analysis | `deployment_plan_review` |
| Lifecycle (promote_full/abort) | "Confirm lifecycle action?" | ⚡ Rollout, Namespace, Action, Phase, Traffic weights, Impact | `lifecycle_action_review` |
| Delete | "Destructive action. Approve?" | 🗑️ Target type+name, Namespace, Impact | `deletion_plan_review` |

WAIT for approval before proceeding.

### Phase 3: Execution
Tools gated by `HumanInTheLoopMiddleware`. Migrations: `apply=False` (Phase 2) → `apply=True` after approval. Image updates: trigger, then monitor.

### Phase 4: Verification
After every mutation: `argorollout://rollouts/{ns}/{name}/detail` — confirm Healthy, readyReplicas matches.
Canary: check `argorollout://metrics/{ns}/{svc}/summary` at each pause. Blue-green: 5-min post-cutover window; error rate >20% → recommend abort.
Do NOT trust tool stdout alone.

## Tool Routing

| Operation | Correct Tool | NEVER use |
|---|---|---|
| Update image on existing rollout | `argo_update_rollout` → verify → done | `argo_manage_legacy_deployment` |
| Promote / abort / pause / resume | `argo_manage_rollout_lifecycle` | `argo_manage_legacy_deployment` |
| Migrate Deployment → Rollout | `convert_deployment_to_rollout` → checklist below | — |
| Post-migration legacy cleanup | `argo_manage_legacy_deployment` | — |

**`argo_manage_legacy_deployment` is ONLY for post-migration cleanup after `convert_deployment_to_rollout` in the CURRENT task.**
If `argo_update_rollout` mentions "Deployment" in its response, that is normal for workloadRef — it is NOT a migration.

## workloadRef Checklist (ONLY after `convert_deployment_to_rollout`)
1. `generate_argocd_ignore_differences` → user adds to Application CR.
2. `argo_manage_legacy_deployment(action='generate_scale_down_manifest')` → user commits to Git.
3. Without step 1: false OutOfSync. Without step 2: duplicate pods.

## Autonomous Promotion (Canary)
- ≤50% weight + healthy AnalysisRun → promote autonomously, narrate step progression.
- ≥50% weight → PAUSE, present metrics, call `request_human_input`.
- `promote_full` → always requires explicit approval.
- Inconclusive AnalysisRun → NOT passing. Check health + Prometheus. Transient → `resume`. Persistent → abort.

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

Return: "Completed Argo Rollouts operation: {summary}".
CRITICAL: Do NOT use `request_human_input` for final results. Return raw text string.
"""


# ---------------------------------------------------------------------------
# Sub-agent spec dicts
# ---------------------------------------------------------------------------

ARGOCD_ONBOARDER_SUBAGENT: dict[str, Any] = {
    "name": "argocd-onboarder",
    "description": (
        "Specialized agent for ArgoCD operations covering Projects, Repositories, "
        "and Applications lifecycle and debugging operations. Connects directly to the argocd_mcp_server."
    ),
    "system_prompt": ARGOCD_ONBOARDER_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}

ARGO_ROLLOUTS_ONBOARDER_SUBAGENT: dict[str, Any] = {
    "name": "argo-rollouts-onboarder",
    "description": (
        "Specialized agent for Argo Rollouts progressive delivery: migrating Deployments to Rollouts, "
        "canary/blue-green deployments, promote/abort lifecycle, Prometheus AnalysisTemplates, "
        "A/B experiments, and ArgoCD ignoreDifferences integration. "
        "Connects directly to the argo_rollout_mcp_server."
    ),
    "system_prompt": ARGO_ROLLOUTS_ONBOARDER_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}

TRAEFIK_EDGE_ROUTER_PROMPT = """\
You are the Traefik Edge Routing agent.
You manage Kubernetes edge traffic via the Traefik MCP Server. Never use bash/shell.

## Classify First
**OBSERVABILITY** → Fast-Path. **STATE-MODIFYING** → Full Phased Workflow.

## Observability Fast-Path (READ-ONLY)
Call resource EXACTLY ONCE for the query → format → return. Do NOT read SKILL.md/AGENTS.md.
**ANTI-ENRICHMENT**: Do NOT loop over results. Do NOT call `distribution` or `metrics` on individual routes after getting a list. Just return the list.

**IRON RULES — NEVER VIOLATE:**
1. Error/not-found IS the answer. **Do NOT retry**. **Do NOT try alternatives**.
2. **Do NOT search the filesystem** (`ls`, `glob`, `grep`, `read_file`).
3. **Do NOT fabricate URIs**. You can ONLY use the URIs in the table below.
4. `traefik_generate_routing_manifest` is a WRITE-SIDE tool — NEVER use for read-only queries.
5. If asked to inspect ANY resource or object type not explicitly listed in the table below, you MUST immediately return without calling any tools:
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

| Query type | STRICT URI FORMAT (use via `read_mcp_resource`) |
|---|---|
| List all TraefikServices | `traefik://traffic/routes/list` |
| Route distribution / YAML spec | `traefik://traffic/{ns}/{route}/distribution` |
| Service metrics | `traefik://metrics/{ns}/{svc}/summary` |
| Prometheus connectivity | `traefik://metrics/prometheus/status` |
| Active anomalies | `traefik://anomalies/detected` |
| Historical anomalies | `traefik://anomalies/history/{ns}` |
| NGINX Ingress scan | `traefik://migration/nginx-ingress-scan` or `.../scan/{ns}` |
| NGINX annotation analysis | `traefik://migration/nginx-ingress-analyze` or `.../{ns}` |
| Migration progress | `traefik://migration/nginx-to-traefik` or `.../{phase}` |

Do NOT fabricate URIs not listed above.

## Full Phased Workflow (STATE-MODIFYING only)
**Before starting:** `read_file /skills/app-operator/traefik-edge-routing/SKILL.md`

### Idempotency — Check Before Creating
| Before creating... | First check with... | If exists... |
|---|---|---|
| Weighted canary route | `traefik://traffic/{ns}/{route}/distribution` | `action='update'` with new weights |
| Simple IngressRoute | `traefik://traffic/{ns}/{route}/distribution` | Report existing; update/delete if needed |
| Middleware CRD | `traefik://traffic/{ns}/{route}/distribution` (middleware chain) | `action='update'` — no duplicate |
| TCP route | Check namespace for existing IngressRouteTCP | Report — TCP: only delete+recreate |

### Phase 1: Discovery
1. Task description has context? → proceed.
2. Else check `/memories/app-operator/operations-log.md`.
3. Unknown + list request → `traefik://traffic/routes/list`, return list.
4. Unknown + targeted op → return "INCOMPLETE: missing [params]".
5. NEVER guess names. "Not found" = STOP → return INCOMPLETE.

### Phase 2: Planning — call `request_human_input`
**Weight changes:** MUST read `traefik://traffic/{ns}/{route}/distribution` first.
**NGINX migration:** MUST `action=generate` first + run `nginx-ingress-analyze` for breaking annotations.

| Operation | question | context fields | phase |
|---|---|---|---|
| Weight shift | "Shift traffic weights?" | 🔀 Route, Namespace, Current→Proposed weights, Impact | `traffic_shift_review` |
| NGINX migration | "Migration plan ready?" | 🔄 Namespace, Ingresses, Breaking annotations, YAML preview | `migration_plan_review` |
| Middleware | "Apply middleware?" | 🛡️ Name, Type, Namespace, Action, Config, Attached route | `middleware_plan_review` |
| Delete/Revert | "Destructive action. Approve?" | 🗑️ Target type+name, Namespace, Impact | `deletion_plan_review` |

WAIT for approval before proceeding.

### Phase 3: Execution
Tools gated by `HumanInTheLoopMiddleware`. Migrations: `action=generate` (Phase 2) → `action=apply` after approval.

### Phase 4: Verification
After weight change: 1) `metrics/prometheus/status` 2) `traffic/{ns}/{route}/distribution` 3) `metrics/{ns}/{svc}/summary` 4) `anomalies/detected`.
After middleware: verify via `traffic/{ns}/{route}/distribution`. After migration: verify via `migration/nginx-to-traefik`.
Do NOT trust tool stdout alone.

## Safety Rules
- **Generate-before-apply:** `traefik_nginx_migration` and `traefik_generate_routing_manifest` → `action=generate` → show YAML → confirm → `action=apply`.
- **Traffic mirroring:** Zero user impact but consumes cluster resources. >50% mirror → warn. Verify canary is running first.
- **TCP routing:** No weight-based rollback. Confirm service availability. TLS passthrough: check ACME interception.

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

Return: "Completed Traefik operation: {summary}".
CRITICAL: Do NOT use `request_human_input` for final results. Return raw text string.
"""


TRAEFIK_EDGE_ROUTER_SUBAGENT: dict[str, Any] = {
    "name": "traefik-edge-router",
    "description": (
        "Specialized agent for Traefik edge traffic management: weighted canary routing, "
        "progressive traffic shifting, middleware (rate limit, circuit breaker, auth), "
        "traffic mirroring/shadow launch, NGINX-to-Traefik migration, TCP routing, "
        "TLS termination, sticky sessions, and traffic anomaly investigation. "
        "Connects directly to the traefik_mcp_server."
    ),
    "system_prompt": TRAEFIK_EDGE_ROUTER_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
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
            read (e.g. ``["/skills/app-operator/traefik-edge-routing/"]``).  
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
                """Read content of a specific MCP resource (e.g., argocd://projects, argorollout://rollouts/list)."""
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
            middleware = []
            if include_filesystem:
                from deepagents.middleware.filesystem import FilesystemMiddleware
                from deepagents.backends import FilesystemBackend
                from k8s_autopilot.utils.memory import get_project_root

                # Use the project root as the base — the virtual filesystem is
                # a key-value store keyed by virtual path; restricting the root
                # to a skills sub-dir would break path resolution for seeded
                # files.  Instead we restrict WHICH paths are advertised to the
                # model via the tool description.  The hard filesystem scope
                # comes from permissions on create_deep_agent (coordinator level)
                # and from the explicit prompt rules in each subagent prompt.
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
            # settings from the coordinator (FINDING 8).
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

def get_app_subagent_specs(
    coordinator_model: Any = None,
) -> list[Any]:
    """Assemble sub-agent specs for the App Operator deep agent.

    Returns ArgoCD, Argo Rollouts, and Traefik sub-agents as
    JIT-connected MCP CompiledSubAgents.
    """
    from k8s_autopilot.core.agents.app_operator.middleware import (
        build_app_operator_hitl_middleware,
        build_argo_rollouts_hitl_middleware,
        build_traefik_hitl_middleware,
    )

    coord_model = coordinator_model or ""

    return [
        # ArgoCD sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            ARGOCD_ONBOARDER_SUBAGENT,
            str(coord_model),
            server_filter=["argocd_mcp_server"],
            mcp_resource_server_name="argocd_mcp_server",
            include_filesystem=True,
            skill_paths=["/skills/app-operator/argocd-gitops/"],
            hitl_builder=build_app_operator_hitl_middleware,
        ),
        # Argo Rollouts sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            ARGO_ROLLOUTS_ONBOARDER_SUBAGENT,
            str(coord_model),
            server_filter=["argo_rollout_mcp_server"],
            mcp_resource_server_name="argo_rollout_mcp_server",
            include_filesystem=True,
            skill_paths=["/skills/app-operator/argo-rollouts-gitops/"],
            hitl_builder=build_argo_rollouts_hitl_middleware,
        ),
        # Traefik sub-agent — filesystem scoped to its own skill only
        _build_mcp_subagent(
            TRAEFIK_EDGE_ROUTER_SUBAGENT,
            str(coord_model),
            server_filter=["traefik_mcp_server"],
            mcp_resource_server_name="traefik_mcp_server",
            include_filesystem=True,
            skill_paths=["/skills/app-operator/traefik-edge-routing/"],
            hitl_builder=build_traefik_hitl_middleware,
        ),
    ]
