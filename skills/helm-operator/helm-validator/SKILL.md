---
name: helm-validator
description: >-
  Validates Helm charts by running helm lint and helm template commands in
  a sandbox. Use when the coordinator delegates chart validation after
  generation or update. Returns VALID or INVALID with structured error details.
  Also use when checking chart syntax, template rendering, or values
  completeness. Triggers on keywords: validate, lint, helm lint, helm template,
  check chart, verify chart, syntax check.
compatibility: >-
  Requires execute tool for shell commands and ls for virtual filesystem
  checks. Helm CLI must be installed and accessible from the project root.
metadata:
  author: talkops-ai
  version: "2.0"
allowed-tools: execute ls read_file
---

# Helm Chart Validator

## When to Use

Use this skill when the coordinator delegates via `task(helm-validator)`
after helm-generator or helm-updater has written files and `sync_workspace`
has materialized them to disk.

## ⚠️ PATH WARNING — READ THIS FIRST

The `ls` and `read_file` tools use **VIRTUAL** absolute paths (starts with `/`).
The `execute` tool runs **REAL** shell commands from the project root.

These are TWO DIFFERENT path systems:
- ✓ CORRECT: `execute("cd workspace/helm-charts/{chart} && helm lint .")`
- ✗ WRONG: `execute("cd /workspace/helm-charts/{chart} && helm lint .")`

The difference: **NO leading slash** in execute paths.

## Reference Files

| Reference | Contents | Read when |
|---|---|---|
| `references/common-errors.md` | Known error patterns and fix instructions | Validation fails |

## Validation Workflow

Progress:
- [ ] Step 1: Verify chart exists on disk
- [ ] Step 2: Run helm lint
- [ ] Step 3: Run helm template
- [ ] Step 4: Determine result

### Step 1. Verify Chart Exists

Before running helm commands, confirm the files are on real disk:
```
execute("ls -la workspace/helm-charts/{chart}/")
```
If the directory is empty or missing, STOP and return:
```
INVALID: chart directory not found or empty at workspace/helm-charts/{chart}/
```

### Step 2. Run Helm Lint

```
execute("cd workspace/helm-charts/{chart} && helm lint .")
```
This catches: missing `Chart.yaml`, malformed YAML, structural violations.

### Step 3. Run Helm Template

```
execute("cd workspace/helm-charts/{chart} && helm template test-release . --debug")
```
This catches: undefined template references, missing `Values.*` keys,
Go template syntax errors, `_helpers.tpl` macro mismatches.

### Step 4. Determine Result

**On success:**
```
VALID: all checks passed (lint ✓, template ✓)
```

**On failure:** Read `references/common-errors.md` for known error patterns,
then return structured errors:
```
INVALID:
  1. [file]: [error description] — suggested fix: [fix]
  2. [file]: [error description] — suggested fix: [fix]
```

## Safety Rules — MUST Follow

1. **Never modify chart files.** You are read-only. If validation fails, return errors — the coordinator routes fixes to helm-generator.

2. **Always run BOTH lint and template.** Lint catches structural issues; template catches rendering issues. One passing does not guarantee the other.

3. **Use real paths in `execute`, virtual paths in `ls`.** Mixing path systems is the #1 cause of "directory not found" false negatives.

4. **Never assume success from partial output.** Read the full output of both commands. A warning in lint may indicate a silent template failure.

## Gotchas

- `helm template` may succeed even if the chart has logical errors (e.g., wrong port number). Template rendering only checks syntax, not semantics.

- If `helm lint` reports "chart directory is not present" but you confirmed files exist via `ls`, the issue is likely a missing `Chart.yaml` or wrong working directory.

- Subchart dependencies require `helm dependency update` before `helm lint`. If lint fails with "Error: found in Chart.yaml, but missing in charts/ directory", the fix is to run `execute("cd workspace/helm-charts/{chart} && helm dependency update")` first.

## Response Format — STRICT

The coordinator depends on these exact prefixes:
- Success: `VALID: all checks passed (lint ✓, template ✓)`
- Failure: `INVALID: [structured error list]`

Never return anything else. Any deviation will break the coordinator's routing logic.
