# Helm Operation Workflows — Detailed Reference

Read this file when you need step-by-step procedures for state-modifying operations.
For read-only queries, this file is NOT needed — use the Query Fast-Path in SKILL.md directly.

## Install Workflow

### 1. Discovery Phase

```
Step 1: Check for existing release (UPGRADE DETECTION)
  → helm_get_release_status(release_name, namespace)
  → If exists: SWITCH to Upgrade Workflow
  → If not found: Continue with install

Step 2: Search for chart
  → helm_search_charts(query="<chart>", repository="<repo>")
  → Identify primary chart match

Step 3: Get chart details
  → helm_get_chart_info(chart_name="<chart>", repository="<repo>")
  → Extract: name, version, description, repository URL
  → IMPORTANT: Extract repository_url for planner

Step 4: Read chart README
  → read_mcp_resource("helm://charts/{repo}/{chart}/readme")
  → Extract: configuration examples, production patterns, required fields

Step 5: Get values schema
  → helm_get_chart_values_schema(chart_name="<chart>", repository="<repo>", version="<ver>")
  → Cross-reference with README
  → Identify truly REQUIRED fields (no defaults, production-critical)

Step 6: Cluster context
  → kubernetes_get_cluster_info()
  → kubernetes_list_namespaces()
  → Check K8s version compatibility, namespace existence, naming conflicts
```

### 2. Values Confirmation (HITL)

```
Step 1: Categorize fields
  → Required Fields (No Defaults): MUST ask user for input
  → Optional Fields (Has Defaults): Ask user to confirm or override

Step 2: Format values template
  ### Required Fields (No Defaults — Input Required)
  - **field.name**: Description
    - Type: <type>
    - Production Impact: <impact>
    - Example: <example_from_readme>

  ### Optional Fields (Confirm or Override Defaults)
  - **field.name**: Description
    - Default: <default_value>
    - Type: <type>

Step 3: Request user input
  → request_human_input(question=<formatted_template>, context="values_confirmation")
  → WAIT for response

Step 4: Process response
  → If user provides YAML values: Use those
  → If user says "approve" / "no changes": Use presented defaults
  → If user provides partial values: Merge with defaults
  → IMMEDIATELY proceed to Planning. Do NOT re-ask.
```

### 3. Planning Phase

```
Step 1: Ensure repository
  → helm_ensure_repository(repo_name="<repo>", repo_url="<url>")
  → CRITICAL: Do this BEFORE any validation to avoid "repo not found" errors

Step 2: Validate values
  → helm_validate_values(chart_name="<repo>/<chart>", values={...})
  → chart_name MUST be in repo/chart format
  → If fails: Report errors and STOP
  → If "repository not found": Call helm_ensure_repository first, retry

Step 3: Render manifests
  → helm_render_manifests(chart_name="<repo>/<chart>", values={...}, version="<ver>")
  → Generate K8s manifests for preview

Step 4: Validate manifests
  → helm_validate_manifests(manifests=<rendered_manifests>)
  → Verify all manifests are valid K8s YAML

Step 5: Check prerequisites
  → kubernetes_check_prerequisites(api_version="<ver>", resources=[...])
  → Verify cluster readiness

Step 6: Generate installation plan
  → helm_get_installation_plan(chart_name="<repo>/<chart>", values={...})
  → Extract: resource estimates, execution steps, rollback strategy
```

### 4. Approval (HITL)

```
Step 1: Format plan for review
  Include ALL fields from planning tools:
  - Chart name, repository, version, release name, namespace
  - Configuration values (YAML format)
  - Validation results
  - Prerequisites check results
  - Resource requirements (CPU, memory, storage WITH units)
  - Numbered execution steps
  - Rollback strategy
  - Monitoring plan
  - Warnings
  NEVER use placeholder text like "Not specified"

Step 2: Request approval
  → request_human_input(question=<formatted_plan>, context="installation_plan_review")
  → WAIT for response
  → If "approve": Proceed to Execution
  → If modifications requested: Return to Planning Phase with updated values
  → If "reject": STOP and report
```

### 5. Execution

```
Step 1: Dry-run (INSTALL ONLY)
  → helm_dry_run_install(chart_name="<repo>/<chart>", release_name="<name>",
      namespace="<ns>", values={...}, version="<ver>")
  → If fails: Report errors and STOP

Step 2: Install
  → helm_install_chart(chart_name="<repo>/<chart>", release_name="<name>",
      namespace="<ns>", values={...}, version="<ver>")
  → HITL middleware auto-pauses for approval card
```

### 6. Verification

```
Step 1: Check release status
  → helm_get_release_status(release_name="<name>", namespace="<ns>")
  → Check status field: "deployed" = success

Step 2: Report
  → Format: "Completed Helm operation: install <release_name>"
  → Include: status, namespace, revision, notes
```

---

## Upgrade Workflow

### Key Differences from Install
- **Upgrade detection**: Release already exists (discovered in Phase 1)
- **Preserve current config**: Use `--reuse-values` by default
- **Only validate changes**: Skip validation for unchanged values
- **No dry-run**: Call `helm_upgrade_release` directly
- **Version handling**: Keep current version unless user explicitly requests version upgrade

### Execution

```
Step 1: Extract user-requested changes
  → Parse user request for value changes (e.g., "set global.domain to talkops.ai")
  → Merge with current configuration from discovery

Step 2: Values Confirmation (if needed)
  → Present current values + requested changes
  → request_human_input for confirmation

Step 3: Planning
  → helm_ensure_repository → helm_validate_values (only changed values)
  → helm_get_installation_plan

Step 4: Approval
  → Present upgrade plan with current vs new comparison

Step 5: Execute
  → helm_upgrade_release(release_name="<name>", chart_name="<repo>/<chart>",
      namespace="<ns>", values={...}, version="<current_or_new>")
  → Middleware auto-pauses for approval

Step 6: Verify
  → helm_get_release_status → confirm "deployed"
```

---

## Rollback Workflow

### Key Differences
- **Skip all validation tools**: Rollback reverts to known-good state
- **Must use real revision numbers**: From `helm_get_release_history`
- **No values confirmation needed**

### Steps

```
Step 1: Get release history
  → helm_get_release_history(release_name="<name>", namespace="<ns>")
  → Extract: current revision, previous revision, timestamps, statuses
  → Identify target revision (default: previous, or user-specified)

Step 2: Create rollback plan (NO validation needed)
  Rollback Plan:
  - Current: Revision <N> (chart v<X.Y.Z>, deployed <date>)
  - Target: Revision <N-1> (chart v<X.Y.Z>, deployed <date>)
  - Command: helm rollback <release> <target_revision> -n <namespace>

Step 3: Approval
  → request_human_input with rollback plan
  → WAIT for approval

Step 4: Execute
  → helm_rollback_release(release_name="<name>", namespace="<ns>",
      revision=<target_revision>)
  → Middleware auto-pauses for approval

Step 5: Verify
  → helm_get_release_status → confirm "deployed" with target revision
```

---

## Uninstall Workflow (Simplified)

```
Step 1: Verify release exists
  → helm_get_release_status(release_name="<name>", namespace="<ns>")
  → If not found: Report "release does not exist" and STOP

Step 2: Execute uninstall
  → helm_uninstall_release(release_name="<name>", namespace="<ns>")
  → Middleware auto-pauses for approval card

Step 3: Verify removal (CRITICAL)
  → helm_get_release_status(release_name="<name>", namespace="<ns>")
  → Check status:
    ❌ If "deployed" or "failed": Uninstall FAILED
    ✅ If "uninstalled" or "not found": Uninstall SUCCEEDED
  → ALWAYS trust verification over tool stdout

Step 4: Report final status
```

---

## Discovery Output Format

When completing Phase 1 (Discovery), output structured JSON:

```json
{
  "scenario_type": "INSTALL | UPGRADE",
  "chart_information": {
    "name": "chart-name",
    "repository": "repo-name",
    "repository_url": "https://...",
    "version": "X.Y.Z",
    "description": "...",
    "source": "helm_get_chart_info + README analysis"
  },
  "current_configuration": {
    "release_name": "...",
    "chart": "repo/chart",
    "version": "X.Y.Z",
    "namespace": "...",
    "user_values_yaml": "...",
    "upgrade_type": "...",
    "user_requested_changes": ["..."]
  },
  "required_configuration": [
    {
      "field": "database.adminPassword",
      "description": "Administrator password",
      "validation": {"type": "string", "default": null},
      "source": "Schema | README | Both",
      "production_impact": "Critical",
      "example_from_readme": "Use strong password (min 16 chars)"
    }
  ],
  "cluster_context": {
    "cluster_version": "1.28.0",
    "target_namespace": {"name": "default", "status": "Exists | New"},
    "existing_releases": 5,
    "potential_conflicts": []
  },
  "next_step": {
    "recommended_action": "Proceed to Values Confirmation",
    "information_sources": ["helm_get_chart_info", "README", "schema"]
  }
}
```
