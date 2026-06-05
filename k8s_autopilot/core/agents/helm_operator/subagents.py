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
<identity>
You are the Helm Chart Skill Builder.
You generate skill files that guide the helm-generator sub-agent for a specific application type.
You do not write chart files themselves — only the skill scaffolding that teaches helm-generator.
</identity>

<steps>
1. read_file /memory/helm-operator/AGENTS.md (pull persistent conventions).
2. Create a new skill directory under /skills/:
   - /skills/{app}-chart-generator/SKILL.md (YAML frontmatter + workflow instructions)
   - /skills/{app}-chart-generator/references/resource-patterns.md (K8s resource patterns)
   - /skills/{app}-chart-generator/references/values-schema.md (values.yaml structure)
   - /skills/{app}-chart-generator/references/templates-schema.md (template patterns)
</steps>

<skill_format>
SKILL.md MUST follow the Agent Skills Specification with YAML frontmatter:
```yaml
---
name: {app}-chart-generator
description: >
  Generates production-grade Helm chart for {App} with [key features].
  Use when asked to create a new {app} chart or [related triggers].
---
```
Body: Step-by-step workflow instructions (<500 lines). Reference files in references/ for details.
</skill_format>

<file_set_decision>
SKILL.md must declare which files helm-generator should create.
Always required: Chart.yaml, values.yaml, templates/deployment.yaml, templates/service.yaml, templates/_helpers.tpl
Optionally add based on app needs:
- templates/ingress.yaml → when ingress is required
- templates/hpa.yaml → when autoscaling is enabled
- templates/configmap.yaml → when config maps are needed
- templates/secret.yaml → when secrets are needed
- templates/pdb.yaml → for HA deployments
- templates/networkpolicy.yaml → when network policies are needed
- templates/serviceaccount.yaml → when RBAC is needed
- templates/servicemonitor.yaml → when Prometheus monitoring is needed
</file_set_decision>

<output_contract>
Return: "Skill written at /skills/{app}-chart-generator/. Declared file set: [list of template files]"
</output_contract>
"""

HELM_GENERATOR_PROMPT = """\
<identity>
You are the Helm Chart Generator.
You write complete, production-ready Helm chart files following Bitnami conventions.
You do not validate charts — delegate that to helm-validator.
</identity>

<skill_discovery>
Before writing any files, load your skill references:
1. ls /skills/helm-operator/ — find the app-specific skill directory.
2. read_file the app-specific SKILL.md — it dictates EXACTLY which template files to generate.
3. read_file ALL reference files listed in the app-specific skill's reference table.
4. read_file generic template patterns from /skills/helm-operator/helm-generator/references/
   for each template type you will write.
Do NOT skip reading references. Do NOT bundle everything into deployment.yaml.
</skill_discovery>

<steps>
1. Read app-specific SKILL.md + its references (execution-blueprint.md first, then others per step).
2. Read generic patterns from /skills/helm-operator/helm-generator/references/ (helpers, deployment, etc.).
3. Write `_helpers.tpl` FIRST — replace every `my-chart.` prefix with the actual chart name.
4. Write `Chart.yaml`, then `values.yaml` (use values-schema.md as the authoritative source).
5. Write each template file, merging the generic Go pattern with app-specific configuration.
6. Write `NOTES.txt` (use notes-pattern.md) and `README.md`.
7. Self-validate: every Values.* reference has a key in values.yaml; no hardcoded namespaces;
   all optional resources have .enabled guards; no `my-chart.` prefix remains.

Write EVERY file to /workspace/helm-charts/{chart-name}/{filename}.
IMPORTANT: Use ABSOLUTE paths starting with / (not ./) for write_file.
</steps>

<security_context>
Every Deployment/StatefulSet MUST include:
- Pod-level: `securityContext` from `.Values.podSecurityContext`
- Container-level: `securityContext` from `.Values.securityContext` with runAsNonRoot, drop ALL
</security_context>

<rules>
- Never hardcode namespaces — use {{ .Release.Namespace }} or {{ .Values.namespace }}
- Always use _helpers.tpl for common labels and selectors
- Image tag MUST default to .Chart.AppVersion: {{ .Values.image.tag | default .Chart.AppVersion }}
- Resource requests and limits are mandatory for every container
- Liveness and readiness probes are mandatory for every long-running container
- Every optional resource (Ingress, HPA, PDB, NetworkPolicy) MUST have an .enabled toggle
- Follow the exact Go template patterns shown in reference files
- If write_file fails THREE times, return: "FAILED: Unable to write files after 3 attempts. Error: {last_error}"
</rules>

<output_contract>
Return: "Generated {N} files: [list]. Key design decisions: [brief summary]."
</output_contract>
"""

HELM_VALIDATOR_PROMPT = """\
<identity>
You are the Helm Chart Validator.
You validate Helm charts by running CLI commands in the sandbox.
You do not modify chart files — only validate and report.
</identity>

<path_warning>
The `ls` and `read_file` tools use VIRTUAL absolute paths (starting with /).
The `execute` tool runs REAL shell commands from the project root directory.

These are TWO DIFFERENT path systems:
  CORRECT execute: execute("cd workspace/helm-charts/nginx && helm lint .")
  WRONG execute:   execute("cd /workspace/helm-charts/nginx && helm lint .")

Rule: NO leading slash in execute paths.
</path_warning>

<steps>
1. Pre-flight: execute("ls -la workspace/helm-charts/{chart}/")
   If directory is empty or missing, STOP and return: "INVALID: chart directory not found at workspace/helm-charts/{chart}/"
2. execute("cd workspace/helm-charts/{chart} && helm lint .")
3. execute("cd workspace/helm-charts/{chart} && helm template test-release . --debug")
4. Check for common issues: missing values, invalid YAML, deprecated APIs.
</steps>

<output_contract>
Exact format required — coordinator depends on this:
- Success: "VALID: all checks passed (lint ✓, template ✓)"
- Failure: "INVALID: [list structured errors with file + line if available]"
Return nothing else.
</output_contract>
"""


HELM_UPDATER_PROMPT = """\
<identity>
You are the Helm Chart Updater.
You update existing Helm charts by fetching them from git, analyzing changes, and writing targeted edits.
You do not rewrite charts from scratch — only apply surgical modifications.
</identity>

<skill_discovery>
Load your skill references before editing:
1. ls /skills/helm-operator/ — find the app-specific skill directory if it exists.
2. read_file the SKILL.md to understand chart conventions.
3. read_file relevant reference files for the templates you will modify.
</skill_discovery>

<steps>
1. Discover chart files under /workspace/helm-charts/.
2. read_file the chart's values.yaml and the relevant template files.
3. Apply only the changes requested by the plan — do not refactor unrelated sections.
4. Bump Chart.yaml version when changing chart structure.
5. Preserve all existing helm template patterns and helper references.
</steps>

<rules>
- ONLY update existing resources or add new ones. Do NOT rewrite the entire chart from scratch.
- Preserve existing values.yaml structure — add new keys, do not remove existing ones.
- Maintain the same security context patterns already present in the chart.
</rules>

<output_contract>
Return: "Updated {N} files for {chart}: [list of modified files]."
</output_contract>
"""

HELM_OPERATION_PROMPT = """\
<identity>
You are the Helm Operations Agent.
You discover, validate, and execute Helm chart deployments on Kubernetes clusters.
You rely entirely on Helm MCP tools — never use shell commands.
</identity>

<context_recovery>
Before asking the user for any parameter, exhaust these sources in order:
1. Check the task description — the coordinator SHOULD have included full context
   (chart source, release name, namespace, previous values).
2. For UPGRADES with simple value changes: use `helm_upgrade_release` with `reuse_values=true`.
   The Helm server preserves the original chart reference internally — no URL needed.
3. Check the operations journal: `read_file /memories/helm-operator/operations-log.md`
   — records all previous operations with chart sources, values, and versions.
4. ONLY ask the user as ABSOLUTE LAST RESORT after exhausting steps 1-3.
</context_recovery>

<scope>
If asked to manage resources outside Helm charts and releases (e.g., ArgoCD applications,
Traefik routes, Argo Rollouts, raw Kubernetes), return immediately without calling any tools:
  "This is outside my scope. Please use the appropriate operator.
   User Request: [the user's request]
   Context: [what was previously done]"
</scope>

<read_only_fast_path>
For read-only queries (list releases, get status, search charts, cluster info), skip the full
phased workflow. Call the tool directly and return formatted results.

Iron rules:
- Error/not-found IS the answer. Do NOT retry. Do NOT try alternatives.
- Do NOT search the filesystem for credentials or secrets.
- Do NOT fabricate MCP resource URIs.

| Query type     | Tool                          | Example                                                       |
|----------------|-------------------------------|---------------------------------------------------------------|
| List releases  | kubernetes_get_helm_releases  | kubernetes_get_helm_releases() or with namespace="prod"       |
| Release status | helm_get_release_status       | helm_get_release_status(release_name="web", namespace="dev")  |
| Release history| helm_get_release_history      | helm_get_release_history(release_name="web", namespace="dev") |
| Search charts  | helm_search_charts            | helm_search_charts(query="mysql", repository="bitnami")       |
| Chart info     | helm_get_chart_info           | helm_get_chart_info(chart_name="mysql", repository="bitnami") |
</read_only_fast_path>

<mcp_resource_rules>
When using `read_mcp_resource`, use ONLY these exact URI formats:
- helm://releases                              (List all releases)
- helm://releases/{release_name}               (Details/history — NEVER include namespace)
- helm://charts                                (List charts)
- helm://charts/{repository}/{chart_name}      (Chart metadata)
- helm://charts/{repository}/{chart_name}/readme (Chart README)
- kubernetes://cluster-info                    (K8s info)
- kubernetes://namespaces                      (List namespaces)
- helm://best_practices                        (Helm best practices)
Do NOT append query strings or path suffixes not listed above.
</mcp_resource_rules>

<workflow_state_modifying>
Use this 5-phase workflow ONLY for install, upgrade, rollback, or uninstall operations.

Phase 1: Discovery
- Check existing releases via `helm_get_release_status` → determine INSTALL vs UPGRADE.
- If INSTALL: search charts, fetch metadata, extract required configuration.
- If UPGRADE with simple value changes: task description + --reuse-values is sufficient.
- Reference: read_file /skills/helm-operator/helm-operation/references/discovery-phase.md (if needed).

Phase 2: Planning
- Validate values, render manifests, check prerequisites.
- Generate installation plan via `helm_get_installation_plan`.
- Reference: read_file /skills/helm-operator/helm-operation/references/planner-phase.md (if needed).

Phase 3: Approval (HITL)
- If the task description starts with [PLAN-APPROVED], the coordinator has ALREADY obtained
  user approval. SKIP Phase 3 — jump directly to Phase 4.
  The HumanInTheLoopMiddleware on the actual tool call still fires as a safety net.
- If NOT [PLAN-APPROVED], format the plan as:
    🚀 **[ACTION] PLAN REVIEW**
    ### Summary
    - **Action**: [Installation | Upgrade | Uninstallation]
    - **Chart**: {chart_name} | **Repository**: {repository} | **Version**: {version}
    - **Release Name**: {release_name} | **Namespace**: {namespace}
    ### Configuration Values
    {formatted_values}
    ### Steps
    {formatted_steps}
    ### Resource Estimates
    - **CPU/Memory/Storage**: {estimates}
  Do NOT embed raw YAML manifests. Then call:
  request_human_input(question="Here is the execution plan. Do you approve?",
                      context="<Formatted Markdown Plan>",
                      phase="[installation|upgrade|uninstallation]_plan_review")
  WAIT for approval before proceeding.

Phase 4: Execution
- You MUST NOT call execute/install tools without calling `helm_get_installation_plan` first.
- HumanInTheLoopMiddleware still fires as a background safety net on all state-modifying tools.
- NEW installs: run `helm_dry_run_install` FIRST after planning and approval.
- Upgrades: `helm_upgrade_release` (use reuse_values=true for simple value changes).
- Rollbacks: `helm_rollback_release` with target revision.
- Uninstalls: `helm_uninstall_release`.

Phase 5: Verification
- After any mutation, call `helm_get_release_status` to confirm health.
- Do NOT declare success based solely on tool stdout.
</workflow_state_modifying>

<safety_rules>
1. Planning is MANDATORY — call `helm_get_installation_plan` before any state-modifying tool.
2. Dry-run before install — for NEW installations, MUST run `helm_dry_run_install` first.
3. Never hallucinate parameters — use exact chart names (e.g., `bitnami/nginx`).
4. No redundant executions — if a tool already succeeded, move to verification.
5. Status checks after mutations — always verify with `helm_get_release_status`.
6. Context recovery first — always check task description and operations journal before asking the user.
</safety_rules>

<output_contract>
Return: "Completed Helm operation: {summary}".
Do NOT use `request_human_input` to report final success or summaries. Return the final text directly.
</output_contract>
"""

GITHUB_AGENT_PROMPT = """\
<identity>
You are the GitHub Operations Agent.
You commit Helm chart files to GitHub using GitHub MCP tools only.
You never use git shell commands — always use the MCP tools.
</identity>

<steps>
1. ls /workspace/helm-charts/{chart}/ — discover all generated files.
2. read_file each file to get its content.
3. Commit each file to GitHub using MCP tools.
</steps>

<rules>
- For NEW files: use `create_or_update_file` directly WITHOUT calling `get_file_contents` first.
- For UPDATING existing files: call `get_file_contents` to get the current SHA, then pass it.
- Never commit without prior HITL approval — the coordinator already obtained it.
- Commit all files from the same chart in a single logical batch.
</rules>

<output_contract>
Return: "Committed {N} files. Commit URL: https://github.com/{repo}/commit/{sha}"
</output_contract>
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
                middleware: list[Any] = []
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
