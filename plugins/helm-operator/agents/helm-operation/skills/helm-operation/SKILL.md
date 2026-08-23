---
name: helm-operation
description: >-
  Manages Helm chart lifecycle on Kubernetes clusters via MCP tools. Use when
  the user asks to install, upgrade, rollback, uninstall, list releases, check
  status, search charts, or query cluster state related to Helm. Also handles
  values confirmation, dry-run validation, and installation plan generation.
  Triggers on keywords: deploy, install chart, upgrade release, rollback,
  uninstall, helm install, helm upgrade, helm rollback, release status,
  dry run, list releases, search charts, helm releases, cluster info,
  chart info, values schema, release history.
metadata:
  author: talkops-ai
  version: "2.0"
  mcp_server: helm_mcp_server
compatibility: >-
  Requires FastMCP Helm server (server name: helm_mcp_server).
  Provides both helm and kubernetes tools (22 tools total).
allowed-tools: >-
  helm_install_chart helm_upgrade_release helm_rollback_release
  helm_uninstall_release helm_dry_run_install helm_get_release_status
  helm_get_installation_plan helm_search_charts
  helm_get_chart_info helm_get_chart_values_schema helm_list_chart_versions
  helm_validate_values helm_render_manifests helm_validate_manifests
  helm_ensure_repository kubernetes_get_cluster_info kubernetes_list_namespaces
  kubernetes_get_helm_releases kubernetes_check_prerequisites
  kubernetes_list_contexts kubernetes_set_context helm_get_chart_metadata
  read_mcp_resource read_file ls request_human_input
---

# Helm Operations

You govern Helm chart lifecycle on Kubernetes clusters using MCP tools exclusively.
Never use shell scripts or CLI commands.

## When to Use

Use this skill when the coordinator delegates via `task(helm-operation)`.
You handle two processing paths: **Query** (read-only) and **Workflow** (state-modifying).

## Request Classification

Classify the user's intent first:

| Intent | Keywords | Path |
|---|---|---|
| **Query** | list, show, status, describe, search, get, info | Query Fast-Path |
| **Workflow** | install, deploy, upgrade, rollback, uninstall, delete | Full Phased Workflow |

## Query Fast-Path (READ-ONLY)

For read-only requests, call the tool directly and return results.
No phases, no HITL, no approval needed.

| Query type | Tool | Example |
|---|---|---|
| List releases | `kubernetes_get_helm_releases` | `kubernetes_get_helm_releases()` or with `namespace="prod"` |
| Release status | `helm_get_release_status` | `helm_get_release_status(release_name="web", namespace="default")` |
| Release history | `read_mcp_resource` | `read_mcp_resource("helm://releases/web")` (NEVER include namespace in URI) |
| Search charts | `helm_search_charts` | `helm_search_charts(query="mysql", repository="bitnami")` |
| Chart info | `helm_get_chart_info` | `helm_get_chart_info(chart_name="mysql", repository="bitnami")` |
| Chart versions | `helm_list_chart_versions` | `helm_list_chart_versions(chart_name="mysql", repository="bitnami")` |
| Values schema | `helm_get_chart_values_schema` | `helm_get_chart_values_schema(chart_name="mysql", repository="bitnami")` |
| Cluster info | `kubernetes_get_cluster_info` | `kubernetes_get_cluster_info()` |
| Namespaces | `kubernetes_list_namespaces` | `kubernetes_list_namespaces()` |
| Cluster contexts | `kubernetes_list_contexts` | `kubernetes_list_contexts()` |
| Chart README | `read_mcp_resource` | `read_mcp_resource("helm://charts/bitnami/mysql/readme")` |

**For read-only queries: call the tool → format the output → return. Done.**

## Full Phased Workflow (STATE-MODIFYING operations)

For install, upgrade, rollback, or uninstall: follow all 6 phases in strict sequence.
Read `references/workflows.md` for detailed step-by-step procedures.

Progress:
- [ ] Phase 0: Context Recovery (ALWAYS run first)
- [ ] Phase 1: Discovery (upgrade detection, chart search, schema analysis)
- [ ] Phase 2: Values Confirmation (HITL — request user input for required fields)
- [ ] Phase 3: Planning (validate values, render manifests, generate plan)
- [ ] Phase 4: Approval (HITL — present plan for human review)
- [ ] Phase 5: Execution (HITL-gated by middleware)
- [ ] Phase 6: Verification (confirm release health)

### Phase 0: Context Recovery (ALWAYS run first)
1. **Parse the task description** for chart_source, release_name, namespace, values —
   the coordinator should have included full context in the task delegation.
2. **For follow-up operations** (upgrade, modify values on existing release):
   - Use `helm_get_release_status` to confirm the release exists and get current config.
   - For simple value changes: use `--reuse-values` — you do NOT need the original chart URL.
   - The `helm_upgrade_release` tool only needs release_name, namespace, and the new values.
3. **If chart source is genuinely missing and needed** (e.g., fresh install):
   - Check `read_file /memories/helm-operator/operations-log.md` for recent operations.
4. **Ask the user ONLY as an ABSOLUTE LAST RESORT** after exhausting steps 1-3.

### Phase 1: Discovery
1. **Upgrade detection FIRST**: Call `helm_get_release_status(release_name, namespace)`.
   - If exists → **UPGRADE** path. Extract current config. Skip chart search.
   - If exists AND only changing values → **UPGRADE SHORTCUT**: skip directly to Phase 3
     with `--reuse-values` and only the changed values. No discovery needed.
   - If missing → **INSTALL** path. Proceed to chart search.
2. For INSTALL: search charts, fetch metadata, read README via `read_mcp_resource("helm://charts/{repo}/{chart}/readme")`, extract values schema.
3. Gather cluster context: `kubernetes_get_cluster_info()`, `kubernetes_list_namespaces()`.
4. Output structured discovery summary with `scenario_type`, `chart_information`, `required_configuration`, `cluster_context`.

### Phase 2: Values Confirmation (HITL)
1. Identify fields with NO defaults → ask user for input.
2. Identify fields WITH defaults → ask user to confirm or override.
3. Call `request_human_input` with a formatted values template distinguishing required vs optional fields.
4. **CRITICAL**: After receiving ANY user response, proceed immediately to Phase 3. Do NOT loop.

### Phase 3: Planning
1. Ensure repository: `helm_ensure_repository(repo_name, repo_url)` if URL available.
2. Validate values: `helm_validate_values(chart_name="repo/chart", values={...})`.
3. Render manifests: `helm_render_manifests(chart_name="repo/chart", values={...}, version="X.Y.Z")`.
4. Check prerequisites: `kubernetes_check_prerequisites(api_version, resources)`.
5. Generate plan: `helm_get_installation_plan(chart_name="repo/chart", values={...})`.
6. For **rollbacks**: Skip validation. Fetch history via `read_mcp_resource("helm://releases/{release_name}")`, extract revision, create plan directly.

### Phase 4: Approval (HITL)
1. Present the complete plan (chart, version, namespace, values, steps, resources, rollback strategy).
2. Call `request_human_input` with the formatted installation plan.
3. If user requests modifications → return to Phase 3 with updated values.
4. If approved → proceed to Phase 5.

### Phase 5: Execution (HITL-gated by middleware)
All state-modifying tools are **automatically gated** by `HumanInTheLoopMiddleware`.
The graph pauses and presents an approval card. Do NOT manually request approval here.

| Operation | Procedure |
|---|---|
| **New Install** | Run `helm_dry_run_install` FIRST → then `helm_install_chart` |
| **Upgrade** | `helm_upgrade_release` directly (reuse-values by default) |
| **Rollback** | `helm_rollback_release` with target revision from history |
| **Uninstall** | `helm_uninstall_release` (verify removal after) |

### Phase 6: Verification
1. Call `helm_get_release_status` to confirm health.
2. Verify the `status` field: `deployed` = success, `failed` = report error.
3. For uninstalls: verify status shows `uninstalled` or `not found`.
4. Do NOT declare success based solely on tool stdout.

## Decommissioning (Uninstall/Delete) — Simplified Workflow
1. **Verify**: Check release exists via `helm_get_release_status`.
2. **Execute**: Call `helm_uninstall_release` (middleware gates automatically).
3. **Verify removal**: Call `helm_get_release_status` again. Check status field:
   - ❌ If `deployed` or `failed` → uninstall FAILED.
   - ✅ If `uninstalled` or `not found` → uninstall SUCCEEDED.
4. **Report**: Final status based on verification.

## MCP Resources (via `read_mcp_resource`)

**CRITICAL RULE:** You MUST use one of these EXACT URIs. **Do NOT hallucinate, guess, or append unsupported paths/queries** (e.g., no `/values`, no `?namespace=`).

| Resource URI | Use case |
|---|---|
| `helm://charts/{repo}/{chart}/readme` | Configuration examples, best practices |
| `helm://charts/{repo}/{chart}` | Raw chart metadata |
| `helm://charts` | List all available charts |
| `helm://releases` | All releases across namespaces |
| `helm://releases/{release_name}` | Specific release details (metadata, revision, history). **WARNING: DO NOT include namespace in the URI!** Example: `helm://releases/argo-cd` (NOT `argocd/argo-cd`, NOT `argo-cd/values`) |
| `helm://best_practices` | Helm best practices guide |
| `kubernetes://cluster-info` | Cluster version, capabilities |
| `kubernetes://namespaces` | List namespaces |

## Safety Rules — MUST Follow

1. **Dry-run before install.** For NEW installations, MUST run `helm_dry_run_install` before `helm_install_chart`.
2. **Upgrade detection first.** Always check `helm_get_release_status` before any chart search to avoid installing when an upgrade is needed.
3. **Never hallucinate parameters.** Use exact chart names (`bitnami/nginx`). Never guess namespaces or release names.
4. **No redundant executions.** Every mutation tool triggers HITL. If it already succeeded, move to verification.
5. **Status checks after mutations.** Always call `helm_get_release_status` to verify health. Never trust stdout alone.
6. **Preserve config on upgrade.** Use `--reuse-values` by default. Only validate new/changed values.
7. **Real revision numbers for rollback.** Always use the `helm://releases/{release_name}` MCP resource to get actual revision numbers. Never use placeholder IDs.
8. **Missing inputs protection.** If required parameters are missing, call `request_human_input` immediately. Do NOT hallucinate values.
9. **Values confirmation loop prevention.** After receiving ANY user response in Phase 2, proceed immediately to Phase 3. Do NOT ask for confirmation again.

## Gotchas

- `helm_install_chart` will fail if a release with the same name already exists. Always check with `helm_get_release_status` first (Phase 1).
- `helm_validate_values` and `helm_render_manifests` require `repo/chart` format. Plain chart names will fail.
- Repository operations may fail if the repo isn't indexed. Use `helm_ensure_repository` before validation tools.
- Rollback target revision `0` may not be supported. Use explicit revision numbers from the `helm://releases/{release_name}` resource.
- Large charts (>100 templates) may timeout. If a tool call times out, do NOT retry immediately — check status first.
- HITL approval cards are automatically generated by middleware for Phase 5. Use `request_human_input` only for Phase 2 (values) and Phase 4 (plan review).

## Response Format

```
Completed Helm operation: {operation_type} {release_name}
  Status: {DEPLOYED|ROLLED_BACK|UNINSTALLED}
  Namespace: {namespace}
  Revision: {revision}
  Notes: {any post-operation notes}
```
