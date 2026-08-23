---
name: helm-coder-agent
description: >-
  Generates new Helm charts from scratch, fetches and surgically edits existing Helm charts,
  validates them using helm lint and helm template, and commits them to GitHub.
  Use for all Helm chart development, modification, validation, and git workflows.
  Triggers on keywords: Helm chart, generate chart, write templates, update chart,
  scaffold, lint, helm template, commit, git push.
compatibility: >-
  Requires write_file, read_file, edit_file, ls, grep, and execute tools.
  Requires GitHub MCP tools for fetching and committing files.
metadata:
  author: talkops-ai
  version: "1.0"
allowed-tools: read_file write_file edit_file ls grep execute github_mcp/*
---

# Helm Coder Agent Skill

This consolidated skill enables generating, updating, validating, and committing Helm charts in a single, unified context.

## Workflow

### 1. Generating a New Chart (from Scratch)
- Read user requirements and parse sizing from `references/helm-architect-guide.md` JSON.
- Write `templates/_helpers.tpl` (referencing `references/helpers-and-values.md`) and `Chart.yaml`.
- Write `values.yaml` (CPU, memory, replicas defaults).
- Write template files to `/workspace/helm-charts/{chart-name}/templates/`:
  - `deployment.yaml` (using `references/deployment-pattern.md`)
  - `service.yaml` (using `references/service-pattern.md`)
  - `ingress.yaml` (if exposing HTTP endpoints, using `references/ingress-pattern.md`)
  - `hpa.yaml` (using `references/autoscaling-pattern.md`)
  - `pdb.yaml` (using `references/pdb-pattern.md`)
  - `networkpolicy.yaml` (using `references/networkpolicy-pattern.md`)
  - `serviceaccount.yaml` (using `references/serviceaccount-pattern.md`)
  - `NOTES.txt` (using `references/notes-pattern.md`)
- Write `README.md` (using `references/readme-pattern.md`).

### 2. Updating an Existing Chart
- Fetch the existing chart from GitHub using GitHub MCP tools (e.g. `get_file_contents` or `list_directory_contents`).
- Write the fetched files to the `/workspace/helm-charts/{chart-name}/` directory.
- Apply surgical edits using `edit_file` to modify only target lines. Preserve indentation, comments, and conditional logic.
- Bump the semantic version in `Chart.yaml`.

### 3. Validating the Chart
- Run validation checks using the `execute` tool:
  - `cd workspace/helm-charts/{chart-name} && helm lint .`
  - `cd workspace/helm-charts/{chart-name} && helm template test-release . --debug`
- If syntax or Go template errors occur, inspect the output, correct the templates, and re-run validation until it passes with zero errors.

### 4. Committing and Pushing to GitHub
- Once validation passes and the changes are complete, commit the files using GitHub MCP tools.
- Do NOT use shell git commands.
