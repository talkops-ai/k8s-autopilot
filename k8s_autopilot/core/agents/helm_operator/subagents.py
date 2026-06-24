"""
Sub-agent specifications for the Helm Operator Deep Agent coordinator.

Each sub-agent is either a dict spec (simple, stateless) or a ``CompiledSubAgent``
(JIT MCP connections, subgraph wrappers). GitHub MCP tools are injected at
runtime via ``build_mcp_subagent()`` — the shared subagent builder.

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
    3. If it needs MCP tools, wrap it with ``build_mcp_subagent()``

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

from k8s_autopilot.core.agents.helm_operator.prompt_sections import (
    compose_helm_operation_prompt,
)

# The helm-operation subagent prompt is composed from modular, testable prompt
# sections registered in prompt_sections.py.  See compose_helm_operation_prompt()
# for the full list of sections and their content.
HELM_OPERATION_PROMPT = compose_helm_operation_prompt()


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
- **Batching with `eval`**: When reading or committing 3+ files, you MUST use the `eval` tool to batch calls.
   **CRITICAL JAVASCRIPT RULES for `eval`**:
   - Do NOT use top-level `return` statements. Leave your final variable as the last line.
   - You MUST `await` all tool calls (e.g., `let res = await tools.get_file_contents(...)`).
   - Tool outputs are usually JSON strings. You MUST `JSON.parse(res)` before accessing properties.
   - Use `let` instead of `const` or `var` in loops to avoid redeclaration errors.
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
    "skills": ["/skills/helm-operator/helm-skill-builder"],
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
    "skills": ["/skills/helm-operator/helm-generator"],
}


HELM_UPDATER_SUBAGENT: dict[str, Any] = {
    "name": "helm-updater",
    "description": (
        "Updates existing Helm chart files. "
        "Use when an existing chart needs to be patched or upgraded instead of created from scratch."
    ),
    "system_prompt": HELM_UPDATER_PROMPT,
    "tools": [],
    "skills": ["/skills/helm-operator/helm-updater"],
}

HELM_OPERATION_SUBAGENT: dict[str, Any] = {
    "name": "helm-operation",
    "description": (
        "Performs live Helm operations (install, upgrade, rollback, uninstall, search) on real clusters. "
        "Connects to helm_mcp_server (which provides both helm and kubernetes tools)."
    ),
    "system_prompt": HELM_OPERATION_PROMPT,
    "tools": [],
    "skills": ["/skills/helm-operator/helm-operation"],
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
    "skills": ["/skills/helm-operator/helm-validator"],
}

# tools=[] here — GitHub MCP tools are merged in build_mcp_subagent() only.
GITHUB_AGENT_SUBAGENT: dict[str, Any] = {
    "name": "github-agent",
    "description": (
        "Commits Helm chart files to GitHub using GitHub MCP server tools. "
        "Returns the commit URL. Never uses shell git commands. "
        "Use after chart-validator confirms VALID and user approves via HITL."
    ),
    "system_prompt": GITHUB_AGENT_PROMPT,
    "tools": [],
    "skills": ["/skills/helm-operator/github-agent"],
}


# ---------------------------------------------------------------------------
# JIT MCP Subagent Wrapper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The shared builder is imported from the central module to avoid 4x duplication.
# See shared_subagent.py for the full implementation (includes SkillsMiddleware).
from k8s_autopilot.core.agents.shared_subagent import build_mcp_subagent

# Helm-specific resource description override for the read_mcp_resource tool.
_HELM_RESOURCE_DESCRIPTION = (
    "Read content of a specific MCP resource by URI "
    "(server: helm_mcp_server). Use this to read "
    "helm releases, chart metadata, and cluster state natively.\n\n"
    "STRICT URI FORMAT RULES:\n"
    "You MUST use exactly one of these formats. DO NOT append `/values`, `?namespace=`, or guess URIs.\n"
    "- `helm://releases`\n"
    "- `helm://releases/[release_name]` (WARNING: namespace filtering is NOT supported. NEVER put namespace in URI)\n"
    "- `helm://charts`\n"
    "- `helm://charts/[repo]/[name]`\n"
    "- `helm://charts/[repo]/[name]/readme`\n"
    "- `kubernetes://cluster-info`\n"
    "- `kubernetes://namespaces`\n"
    "- `helm://best_practices`"
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
    from k8s_autopilot.core.agents.shared_middleware import (
        make_subagent_interpreter_builder,
        HELM_PTC_ALLOWLIST,
        GITHUB_PTC_ALLOWLIST,
    )
    from k8s_autopilot.core.tools.kubectl_tools import create_kubectl_readonly_tool

    val_model = validator_model or coordinator_model
    coord_model = coordinator_model or ""

    return [
        # ── Static sub-agents (simple dict specs) ─────────────────────────
        {**HELM_SKILL_BUILDER_SUBAGENT, "model": coord_model, "tools": []},
        {**HELM_GENERATOR_SUBAGENT, "model": coord_model, "tools": []},
        {**HELM_UPDATER_SUBAGENT, "model": coord_model, "tools": []},
        {**HELM_VALIDATOR_SUBAGENT, "model": val_model, "tools": []},

        # ── JIT MCP sub-agents (lazy connections) ─────────────────────────
        build_mcp_subagent(
            GITHUB_AGENT_SUBAGENT,
            server_filter=["github_mcp"],
            mcp_resource_server_name="github_mcp",
            include_filesystem=True,
            skill_paths=["/skills/helm-operator/"],
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=GITHUB_PTC_ALLOWLIST,
                ),
            ],
        ),
        build_mcp_subagent(
            HELM_OPERATION_SUBAGENT,
            server_filter=["helm_mcp_server"],
            mcp_resource_server_name="helm_mcp_server",
            include_filesystem=True,
            skill_paths=["/skills/helm-operator/helm-operation/"],
            hitl_builder=build_helm_hitl_middleware,
            resource_description_override=_HELM_RESOURCE_DESCRIPTION,
            extra_middleware_builders=[
                make_subagent_interpreter_builder(
                    ptc_allowlist=HELM_PTC_ALLOWLIST,
                ),
            ],
            extra_tools=[create_kubectl_readonly_tool()],
        ),


        # NOTE: helm-planner CompiledSubAgent is added by the coordinator
        # in get_subagent_specs() — it's a subgraph wrapper, not a dict spec.
    ]
