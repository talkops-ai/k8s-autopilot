---
name: helm-updater
description: >-
  Fetches existing Helm charts via GitHub MCP, and applies localized surgical
  edits to templates and values without full rewrites. Use when the coordinator
  delegates an update to an existing chart that needs patching, version bumping,
  or targeted configuration changes. Also use when modifying specific template
  files or values.yaml sections. Triggers on keywords: update chart, patch,
  modify, bump version, edit values, change template, upgrade chart.
compatibility: >-
  Requires read_file, write_file, edit_file, ls, grep tools. GitHub MCP tools
  (get_file_contents, create_or_update_file) for fetching existing charts.
metadata:
  author: talkops-ai
  version: "2.0"
allowed-tools: read_file write_file edit_file ls grep
---

# Helm Chart Updater

## When to Use

Use this skill when the coordinator delegates via `task(helm-updater)` to
modify an existing chart. You perform **surgical edits** — never rewrite
an entire chart from scratch.

## Workflow

Progress:
- [ ] Step 1: Fetch existing chart from GitHub
- [ ] Step 2: Apply surgical edits
- [ ] Step 3: Bump semantic version
- [ ] Step 4: Return summary

### Step 1. Fetch Existing Chart

Use GitHub MCP tools to fetch the chart:
```
list_directory_contents(repo, chart_path)
```
For each file, fetch content AND its SHA:
```
get_file_contents(repo, file_path)
```
Write files to the local virtual workspace:
```
write_file /workspace/helm-charts/{chart}/{filename}
```

### Step 2. Surgical Edits

Use `edit_file` to modify only the lines that need changes.
- Preserve all existing indentation and formatting
- Never use `write_file` to overwrite a file unless generating a completely new resource
- If editing arrays in `values.yaml`, ensure adjacent structures are preserved

### Step 3. Semantic Version Bumping

You MUST update `Chart.yaml` after every edit:
- **Patch** (`1.0.0` → `1.0.1`): bug fixes, minor config changes
- **Minor** (`1.0.0` → `1.1.0`): new features, added resources
- **Major** (`1.0.0` → `2.0.0`): breaking changes to values schema

If the application image tag changed, bump the `appVersion` field as well.

### Step 4. Return Summary

```
Updated {N} files for {chart}. Bumped version to X.Y.Z.
Changes made: [diff summary per file]
```

## Safety Rules — MUST Follow

1. **Never rewrite entire files.** Use `edit_file` for targeted changes. Full rewrites risk losing comments, formatting, and conditional logic.

2. **Always bump Chart.yaml version.** Every chart update MUST include a version bump. Helm uses the version to determine if an upgrade is needed.

3. **Preserve existing conditionals.** If a template has `{{- if .Values.X.enabled }}` guards, do not remove them when editing.

4. **Get SHA before updating remote files.** When the github-agent commits your changes, it needs the SHA. Ensure you fetched SHAs in Step 1.

## Gotchas

- `edit_file` may fail if the target text doesn't match exactly (whitespace-sensitive). Always `read_file` first to get the exact content before editing.

- When adding new values to `values.yaml`, add them at the END of the relevant section. Inserting in the middle risks breaking array indexing.

- Subchart values are namespaced. If updating a `postgresql` subchart value, edit under the `postgresql:` key, not at the root level.

## Response Format

```
Updated {N} files for {chart}. Bumped version to {version}.
Changes:
  - values.yaml: added ingress.tls.secretName default
  - templates/ingress.yaml: added TLS block
  - Chart.yaml: version 1.0.0 → 1.1.0
```
