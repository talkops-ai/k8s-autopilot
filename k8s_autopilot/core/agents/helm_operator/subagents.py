"""
Sub-agent specifications for the Helm Operator Deep Agent coordinator.

Each sub-agent is either a dict spec (simple, stateless) or a ``CompiledSubAgent``
(JIT MCP connections, subgraph wrappers). GitHub MCP tools are injected at
runtime via ``_build_mcp_subagent()`` — identical to the reference pattern.

Sub-agents (chart-generation pipeline only):
    helm-skill-builder  — generates SKILL.md directories for new chart types
    helm-generator      — writes Helm chart files using loaded skill references
    chart-validator     — runs helm lint / helm template in sandbox
    github-agent        — commits chart files to GitHub via GitHub MCP (JIT)

Helm management and ArgoCD are handled by their own standalone coordinators
in ``helm_mgmt/coordinator.py`` and ``onboarding/coordinator.py``.

Extensibility:
    To add a new sub-agent:
    1. Define its prompt and dict spec below
    2. Add it to ``get_helm_subagent_specs()``
    3. If it needs MCP tools, wrap it with ``_build_mcp_subagent()``

Reference: aws-orchestrator-agent tf_operator/subagents.py
"""

from typing import Any, Callable, List, Optional, cast

from k8s_autopilot.utils.logger import AgentLogger

_subagent_logger = AgentLogger("HelmSubagentFactory")


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

HELM_SKILL_BUILDER_PROMPT = """\
You are the Helm Chart Skill Builder.
You generate skill files that guide the helm-generator subagent for a specific application type.

## Steps
1. read_file /memory/helm-operator/AGENTS.md  (pull persistent conventions)
2. Based on the request, create a new skill directory under /skills/:
   - /skills/{app}-chart-generator/SKILL.md (YAML frontmatter + workflow instructions)
   - /skills/{app}-chart-generator/references/resource-patterns.md (K8s resource patterns)
   - /skills/{app}-chart-generator/references/values-schema.md (values.yaml structure)
   - /skills/{app}-chart-generator/references/templates-schema.md (template patterns)

## SKILL.md Format (MUST follow Agent Skills Specification)
```yaml
---
name: {app}-chart-generator
description: >
  Generates production-grade Helm chart for {App} with [key features].
  Use when asked to create a new {app} chart or [related triggers].
---
```
Body: Step-by-step workflow instructions (<500 lines). Reference files in references/ for details.

## File set decision — SKILL.md must declare
Always create: Chart.yaml, values.yaml, templates/deployment.yaml, templates/service.yaml, templates/_helpers.tpl
Optionally add based on app needs:
  - templates/ingress.yaml → when ingress is required
  - templates/hpa.yaml → when autoscaling is enabled
  - templates/configmap.yaml → when config maps are needed
  - templates/secret.yaml → when secrets are needed
  - templates/pdb.yaml → for HA deployments
  - templates/networkpolicy.yaml → when network policies are needed
  - templates/serviceaccount.yaml → when RBAC is needed
  - templates/servicemonitor.yaml → when Prometheus monitoring is needed

## Output
Return: "Skill written at /skills/{app}-chart-generator/. Declared file set: [list of template files]"
"""

HELM_GENERATOR_PROMPT = """\
You are the Helm Chart generator.
You write complete, production-ready Helm chart files following Bitnami conventions.

## Critical First Step — Skill Discovery
1. Use `ls /skills/helm-operator/` to find the app-specific skill directory.
2. `read_file` the app-specific `SKILL.md` — it dictates EXACTLY which template files to generate.
3. `read_file` ALL reference files listed in the app-specific skill's reference table.
4. `read_file` the generic template patterns from `/skills/helm-operator/helm-generator/references/`
   that correspond to the templates you need to write.

Do NOT skip reading references. Do NOT just bundle everything into deployment.yaml.

## Steps
1. Read app-specific SKILL.md + its references (execution-blueprint.md first, then others per step).
2. Read generic patterns from /skills/helm-operator/helm-generator/references/ (helpers, deployment, etc.).
3. Write `_helpers.tpl` FIRST — replace every `my-chart.` prefix with the actual chart name.
4. Write `Chart.yaml`, then `values.yaml` (use values-schema.md as the authoritative source).
5. Write each template file, merging the generic Go pattern with app-specific configuration.
6. Write `NOTES.txt` (use notes-pattern.md) and `README.md`.
7. Self-validate: verify every Values.* reference has a key in values.yaml; verify no hardcoded
   namespaces; verify all optional resources have `.enabled` guards; verify no `my-chart.` remains.

Write EVERY file to /workspace/helm-charts/{chart-name}/{filename}
IMPORTANT: Use ABSOLUTE paths starting with / (not ./) for write_file.

## Security Context — Mandatory
Every Deployment/StatefulSet MUST include:
- Pod-level: `securityContext` from `.Values.podSecurityContext`
- Container-level: `securityContext` from `.Values.securityContext` with runAsNonRoot, drop ALL

## Write Failure Guard
If write_file fails with a path error THREE times, STOP immediately and return:
"FAILED: Unable to write files to /workspace/helm-charts/{chart}/ after 3 attempts. Error: {last_error}"

## Rules
- Never hardcode namespaces — use {{ .Release.Namespace }} or {{ .Values.namespace }}
- Always use _helpers.tpl for common labels and selectors
- Image tag MUST default to .Chart.AppVersion: {{ .Values.image.tag | default .Chart.AppVersion }}
- Resource requests and limits are mandatory for every container
- Liveness and readiness probes are mandatory for every long-running container
- Every optional resource (Ingress, HPA, PDB, NetworkPolicy) MUST have an .enabled toggle
- Follow the exact Go template patterns shown in reference files

## Output
Return: "Generated {N} files: [list]. Key design decisions: [brief summary]."
"""

HELM_VALIDATOR_PROMPT = """\
You are the Helm chart validation specialist.
You validate Helm charts by running CLI commands in the sandbox.

## ⚠️ PATH WARNING — READ THIS FIRST
The `ls` and `read_file` tools use VIRTUAL absolute paths (starting with /).
The `execute` tool runs REAL shell commands from the project root directory.

These are TWO DIFFERENT path systems:
  ✓ CORRECT execute command:  execute("cd workspace/helm-charts/nginx && helm lint .")
  ✗ WRONG execute command:    execute("cd /workspace/helm-charts/nginx && helm lint .")

The difference: NO leading slash in execute paths.

## Pre-validation: verify files exist
Before running helm commands, first run:
  execute("ls -la workspace/helm-charts/{chart}/")
If the directory is empty or missing, STOP and return:
"INVALID: chart directory not found or empty at workspace/helm-charts/{chart}/"

## Validation Steps
1. execute("cd workspace/helm-charts/{chart} && helm lint .")
2. execute("cd workspace/helm-charts/{chart} && helm template test-release . --debug")
3. Check for common issues: missing values, invalid YAML, deprecated APIs

## Output — STRICT FORMAT
On success: "VALID: all checks passed (lint ✓, template ✓)"
On failure: "INVALID: [list structured errors with file + line if available]"
Never return anything else. The coordinator depends on this exact format.
"""


HELM_UPDATER_PROMPT = """\
You are the Helm Chart Updater.
You update existing Helm charts by fetching them from git, analyzing changes, and writing updates.
Your skill dictates the exact modification workflows for existing charts.

## Steps
1. Discover the requested chart files under /workspace/helm-charts/.
2. Read the chart's values.yaml and deployment logic.
3. Update the necessary files (e.g., bump version in Chart.yaml, add values, etc.) as requested by the plan.
4. ONLY update existing resources or add new ones natively. Do NOT rewrite the entire chart from scratch.

Return: "Updated {N} files for {chart}."
"""

HELM_OPERATION_PROMPT = """\
You are the Helm Operations Agent.
You discover, validate, and execute Helm chart deployments on Kubernetes clusters.
You rely entirely on Helm MCP tools — never use shell commands.

## Context Recovery — CRITICAL (Read This First)
Before asking the user for chart source, repository, or any parameter:
1. **Check the task description** — the coordinator SHOULD have included full context
   (chart source, release name, namespace, previous values) in the task delegation.
2. **For UPGRADES to existing releases**: you do NOT need the original chart URL for
   simple value changes. Use `helm_upgrade_release` with `reuse_values=true`,
   providing only the release_name, namespace, and the NEW values to change.
   The Helm server preserves the original chart reference internally.
3. **Check the operations journal**: `read_file /memories/helm-operator/operations-log.md`
   — this file records all previous operations with chart sources, values, and versions.
4. **ONLY ask the user as an ABSOLUTE LAST RESORT** after exhausting steps 1-3.

## Query Fast-Path (READ-ONLY operations)

For **read-only** queries (list releases, get status, search charts, cluster info),
skip the full phased workflow. Just call the tool directly and return results:

**IRON RULES — NEVER VIOLATE:**
1. Error/not-found IS the answer. **Do NOT retry**. **Do NOT try alternatives**.
2. **Do NOT search the filesystem** for credentials or secrets.
3. **Do NOT fabricate URIs**. You can ONLY use the URIs in the table below.
4. If asked to inspect, manage, or fetch credentials for ANY resource or application not explicitly related to Helm charts and releases (e.g., ArgoCD passwords, Traefik routes, Argo Rollouts), you MUST immediately return without calling any tools:
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

| Query type | Tool | Example |
|---|---|---|
| List releases | `kubernetes_get_helm_releases` | `kubernetes_get_helm_releases()` or with `namespace="prod"` |
| Release status | `helm_get_release_status` | `helm_get_release_status(release_name="web", namespace="default")` |
| Release history | `helm_get_release_history` | `helm_get_release_history(release_name="web", namespace="default")` |
| Search charts | `helm_search_charts` | `helm_search_charts(query="mysql", repository="bitnami")` |
| Chart info | `helm_get_chart_info` | `helm_get_chart_info(chart_name="mysql", repository="bitnami")` |

**For read-only queries: call the tool → format the output → return. Done. No phases needed.**

## STRICT MCP Resource Rules (for `read_mcp_resource`)
When using `read_mcp_resource`, you MUST use one of these exact URI formats.
Do NOT guess, hallucinate, or append paths/query strings (like `/values` or `?namespace=`).
- `helm://releases` (List all releases)
- `helm://releases/{release_name}` (Release details/history. **NEVER** include the namespace in the URI)
- `helm://charts` (List charts)
- `helm://charts/{repository}/{chart_name}` (Chart metadata)
- `helm://charts/{repository}/{chart_name}/readme` (Chart README)
- `kubernetes://cluster-info` (K8s info)
- `kubernetes://namespaces` (List namespaces)
- `helm://best_practices` (Helm best practices)

## Full Phased Workflow (STATE-MODIFYING operations only)

Use this workflow ONLY for install, upgrade, rollback, or uninstall operations.
For detailed phase references, read from `/skills/helm-operator/helm-operation/references/`.

### Phase 1: Discovery
- Check existing releases via `helm_get_release_status` → determine INSTALL vs UPGRADE.
- If INSTALL: search charts, fetch metadata, extract required configuration.
- If UPGRADE with simple value changes: you already have what you need from the
  task description + `--reuse-values`. Skip chart search entirely.
- Reference: `references/discovery-phase.md` (read only if needed).

### Phase 2: Planning
- Validate values, render manifests, check prerequisites.
- Generate installation plan via `helm_get_installation_plan`.
- Reference: `references/planner-phase.md` (read only if needed).

        ### Phase 3: Approval (HITL)
        - After generating the installation plan (or uninstall plan), you MUST format the plan EXACTLY as this Markdown template, completely omitting any raw YAML or manifests:

          🚀 **[ACTION] PLAN REVIEW**
          ### Summary
          - **Action**: [Installation | Upgrade | Uninstallation]
          - **Chart**: {chart_name}
          - **Repository**: {repository}
          - **Version**: {version}
          - **Release Name**: {release_name}
          - **Namespace**: {namespace}
          ### Configuration Values
          {formatted_values}
          ### Steps
          {formatted_steps}
          ### Resource Estimates
          - **CPU/Memory/Storage**: {estimates}

        - You MUST NOT embed or include the `manifests_preview` raw YAML anywhere. Let the backend handle the details.
        - You MUST explicitly call `request_human_input(question="Here is the execution plan. Do you approve?", context="<Formatted Markdown Plan>", phase="[action]_plan_review")` \
          (For example, use `installation_plan_review`, `uninstallation_plan_review`, or `upgrade_plan_review` so the UI title is correct).
        - WAIT for the user to approve the plan before proceeding to Execution.
        - Do NOT call execute/install tools without explicitly receiving user approval first.

        ### Phase 4: Execution
        - All state-modifying tools are still safely gated by `HumanInTheLoopMiddleware` as a background fallback.
        - You MUST NOT call execute/install tools without calling `helm_get_installation_plan` and receiving approval first.
        - NEW installs: ALWAYS run `helm_dry_run_install` FIRST after planning and approval.
        - Upgrades: `helm_upgrade_release` directly (use reuse_values=true for simple changes).
        - Rollbacks: `helm_rollback_release` with target revision.
        - Uninstalls: `helm_uninstall_release`.

        ### Phase 5: Verification
        - After any mutation, call `helm_get_release_status` to confirm health.
        - Do NOT declare success based solely on tool stdout.

        ## Safety Rules
        1. Planning is MANDATORY. You MUST call `helm_get_installation_plan` before invoking any state-modifying tools like `helm_install_chart` or `helm_upgrade_release`. Do not skip Phase 2.
        2. Dry-run before install. For NEW installations, MUST run `helm_dry_run_install` first.
        3. Never hallucinate parameters. Use exact chart names (e.g., `bitnami/nginx`).
        4. No redundant executions. If a tool already succeeded, move to verification.
        5. Status checks after mutations. Always verify with `helm_get_release_status`.
        6. Context recovery before user queries. ALWAYS check task description and operations journal before asking the user for missing info.

        Return: "Completed Helm operation: {summary}".
        CRITICAL: Do NOT use `request_human_input` to report final success or summaries. Just return the final raw text string!
"""

GITHUB_AGENT_PROMPT = """\
You are the GitHub operations agent. You commit Helm chart files using GitHub MCP tools only.
Never use git shell commands — always use the MCP tools.

## Key Rules
- For NEW files: use `create_or_update_file` directly WITHOUT calling `get_file_contents` first.
- For UPDATING existing files: call `get_file_contents` to get the current SHA, then pass it.
- Never commit without prior HITL approval
- Commit all files in a single batch

## Steps
1. Use `ls /workspace/helm-charts/{chart}/` to discover all generated files
2. Use `read_file` for each file to get its content
3. Commit each file to GitHub using MCP tools

## Output
Return: "Committed {N} files. Commit URL: https://github.com/{repo}/commit/{sha}"
"""


# ---------------------------------------------------------------------------
# Sub-agent spec dicts
# ---------------------------------------------------------------------------

HELM_SKILL_BUILDER_SUBAGENT: dict[str, Any] = {
    "name": "helm-skill-builder",
    "description": (
        "Generates a per-application skill directory under /skills/ with SKILL.md (YAML frontmatter "
        "+ workflow instructions) and references/ for K8s resource patterns, values schema, and template patterns. "
        "Only needed when no skill exists for the requested app type. "
        "Reads /memory/helm-operator/AGENTS.md for persistent conventions."
    ),
    "system_prompt": HELM_SKILL_BUILDER_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}

HELM_GENERATOR_SUBAGENT: dict[str, Any] = {
    "name": "helm-generator",
    "description": (
        "Writes Helm chart files for a new application. "
        "Reads its skill's SKILL.md and references for template patterns, values schema, and resource structure. "
        "Use for all new chart creation. Do NOT use for managing existing releases."
    ),
    "system_prompt": HELM_GENERATOR_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}


HELM_UPDATER_SUBAGENT: dict[str, Any] = {
    "name": "helm-updater",
    "description": (
        "Updates existing Helm chart files. "
        "Use when an existing chart needs to be patched or upgraded instead of created from scratch."
    ),
    "system_prompt": HELM_UPDATER_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}

HELM_OPERATION_SUBAGENT: dict[str, Any] = {
    "name": "helm-operation",
    "description": (
        "Performs live Helm operations (install, upgrade, rollback, uninstall, search) on real clusters. "
        "Connects to helm_mcp_server (which provides both helm and kubernetes tools)."
    ),
    "system_prompt": HELM_OPERATION_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}

HELM_VALIDATOR_SUBAGENT: dict[str, Any] = {
    "name": "helm-validator",
    "description": (
        "Runs helm lint and helm template on a chart in the sandbox. "
        "Returns VALID or INVALID with structured error details. "
        "Use after helm-generator writes files."
    ),
    "system_prompt": HELM_VALIDATOR_PROMPT,
    "tools": [],
    "skills": ["/skills/"],
}

# tools=[] here — GitHub MCP tools are merged in _build_mcp_subagent() only.
GITHUB_AGENT_SUBAGENT: dict[str, Any] = {
    "name": "github-agent",
    "description": (
        "Commits Helm chart files to GitHub using GitHub MCP server tools. "
        "Returns the commit URL. Never uses shell git commands. "
        "Use after chart-validator confirms VALID and user approves via HITL."
    ),
    "system_prompt": GITHUB_AGENT_PROMPT,
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
    hitl_builder: Optional[Callable[[], Any]] = None,
) -> Any:  # CompiledSubAgent
    """
    Wraps a static dict spec into a dynamic CompiledSubAgent that opens its
    MCP connection Just-In-Time (JIT) specifically when its node is executed.

    Args:
        spec: Static subagent dict (name, description, system_prompt).
        coordinator_model_name: Model name string.
        server_filter: MCP server names to connect to (e.g. ["helm_mcp_server"]).
        mcp_resource_server_name: Server name passed to ``read_mcp_resource``.
        include_filesystem: If True, attach ``FilesystemMiddleware`` backed
            by a ``FilesystemBackend`` pointed at the project root.
        hitl_builder: Callable that returns a ``HumanInTheLoopMiddleware``
            instance. If None, no HITL middleware is attached.

    Reference: aws-orchestrator-agent tf_operator/subagents.py _build_mcp_subagent
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

        try:
            # Lazily connect to MCP right before execution
            async with create_mcp_client(Config(), server_filter=server_filter) as mcp_client:
                tools = mcp_client.get_tools()

                from k8s_autopilot.core.hitl.tools import create_hitl_tools
                from langchain_core.tools import StructuredTool

                # Generic MCP resource reader — parameterized by server_name
                _res_server = mcp_resource_server_name

                async def read_mcp_resource(uri: str) -> str:
                    """Read content of a specific MCP resource by URI.

                    STRICT URI FORMAT RULES:
                    You MUST use exactly one of these formats. DO NOT append `/values`, `?namespace=`, or guess URIs.
                    - `helm://releases`
                    - `helm://releases/[release_name]` (WARNING: namespace filtering is NOT supported. NEVER put namespace in URI)
                    - `helm://charts`
                    - `helm://charts/[repo]/[name]`
                    - `helm://charts/[repo]/[name]/readme`
                    - `kubernetes://cluster-info`
                    - `kubernetes://namespaces`
                    - `helm://best_practices`
                    """
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
                            f"(server: {_res_server}). Use this to read "
                            "helm releases, chart metadata, and cluster state natively."
                        ),
                    )
                )

                # Build middleware list
                middleware = []
                if include_filesystem:
                    from deepagents.middleware.filesystem import FilesystemMiddleware
                    from deepagents.backends import FilesystemBackend
                    from k8s_autopilot.utils.memory import get_project_root

                    root = str(get_project_root())
                    middleware.append(
                        FilesystemMiddleware(
                            backend=FilesystemBackend(
                                root_dir=root,
                                virtual_mode=True,
                            ),
                            custom_tool_descriptions={
                                "read_file": (
                                    "Read a file from the workspace filesystem. "
                                    "Use this to read the EXACT content of generated "
                                    "Helm chart files before committing them to GitHub. "
                                    "ALWAYS use this tool — never guess file contents."
                                ),
                                "ls": (
                                    "List files in a workspace directory. "
                                    "Use this to discover all generated Helm chart files "
                                    "under /workspace/helm-charts/{chart}/."
                                ),
                            },
                        )
                    )
                    _subagent_logger.info(
                        f"{name}: attached FilesystemMiddleware "
                        f"with FilesystemBackend(root_dir={root!r})",
                    )

                if hitl_builder is not None:
                    from langchain.agents.middleware import ToolRetryMiddleware

                    class CustomToolRetryMiddleware(ToolRetryMiddleware):
                        def _should_retry_tool(self, tool_name: str) -> bool:
                            # Never wrap or intercept HITL tools so that their GraphInterrupt bubbles up naturally.
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

                # Lazily instantiate model and graph
                cfg = Config()
                model = create_model(cfg.get_llm_deepagent_config())
                agent_graph = create_agent(
                    model=model,
                    tools=tools,
                    middleware=middleware,
                    system_prompt=system_prompt,
                    name=name,
                )

                # Execute the LangGraph subagent synchronously with the open connection
                result = await agent_graph.ainvoke(cast(Any, state), config)
                return dict(result)

        except Exception as exc:
            # ── Let HITL interrupts propagate normally ────────────────
            # GraphInterrupt is NOT an error — it's the standard
            # control flow for interrupt()/HITL gates.  Re-raise so
            # the coordinator and supervisor can pause and wait for
            # user input.
            from langgraph.errors import GraphInterrupt
            if isinstance(exc, GraphInterrupt):
                raise

            # ── Surface MCP connection failures gracefully ────────────
            # Instead of letting TaskGroup / auth errors crash the
            # coordinator, return a meaningful error message as the
            # subagent's output so the coordinator LLM can report it.
            from langchain_core.messages import AIMessage

            err_str = str(exc)
            _subagent_logger.error(
                f"{name}: MCP subagent execution failed",
                extra={"error": err_str, "servers": server_filter},
            )

            # Build a clear error message for the coordinator
            if any(kw in err_str.lower() for kw in (
                "authentication failed", "401", "403",
                "unauthorized", "forbidden", "expired",
            )):
                error_msg = (
                    f"FAILED: {name} could not connect to the MCP server "
                    f"({', '.join(server_filter)}). The authentication token "
                    f"appears to be expired or invalid. Please generate a new "
                    f"GitHub Personal Access Token and update the "
                    f"GITHUB_PERSONAL_ACCESS_TOKEN environment variable."
                )
            else:
                error_msg = (
                    f"FAILED: {name} encountered an error: {err_str}. "
                    f"The MCP server(s) {server_filter} may be unreachable."
                )

            # Return state with error message so the coordinator
            # receives it via the subagent's output messages.
            messages = list(state.get("messages", []))
            messages.append(AIMessage(content=error_msg))
            return {**state, "messages": messages}

    return CompiledSubAgent(
        name=name,
        description=description,
        runnable=RunnableLambda(_mcp_runnable).with_config({"run_name": name}),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_helm_subagent_specs(
    coordinator_model: Any = None,
    validator_model: Any = None,
) -> list[Any]:
    """
    Assemble sub-agent specs for the Helm Operator deep agent.

    Chart-generation pipeline only. Helm management and ArgoCD are
    handled by their own standalone coordinators.

    Static agents use simple dicts, while MCP-dependent agents are dynamically
    wrapped as CompiledSubAgents for JIT connections.

    Args:
        coordinator_model: Model instance for coordinator-tier sub-agents.
        validator_model: Model instance for the validator (cheaper/faster).
                         Defaults to ``coordinator_model`` if not provided.

    Returns:
        List of mixed sub-agent specs ready for ``create_deep_agent``.
    """
    from k8s_autopilot.core.agents.helm_operator.middleware import (
        build_helm_hitl_middleware,
    )

    val_model = validator_model or coordinator_model
    coord_model = coordinator_model or ""

    return [
        # ── Static sub-agents (simple dict specs) ─────────────────────────
        {**HELM_SKILL_BUILDER_SUBAGENT, "model": coord_model, "tools": []},
        {**HELM_GENERATOR_SUBAGENT, "model": coord_model, "tools": []},
        {**HELM_UPDATER_SUBAGENT, "model": coord_model, "tools": []},
        {**HELM_VALIDATOR_SUBAGENT, "model": val_model, "tools": []},

        # ── JIT MCP sub-agents (lazy connections) ─────────────────────────
        _build_mcp_subagent(
            GITHUB_AGENT_SUBAGENT,
            str(coord_model),
            server_filter=["github_mcp"],
            mcp_resource_server_name="github_mcp",
            include_filesystem=True,
        ),
        _build_mcp_subagent(
            HELM_OPERATION_SUBAGENT,
            str(coord_model),
            server_filter=["helm_mcp_server"],
            mcp_resource_server_name="helm_mcp_server",
            include_filesystem=True,
            hitl_builder=build_helm_hitl_middleware,
        ),


        # NOTE: helm-planner CompiledSubAgent is added by the coordinator
        # in get_subagent_specs() — it's a subgraph wrapper, not a dict spec.
    ]
