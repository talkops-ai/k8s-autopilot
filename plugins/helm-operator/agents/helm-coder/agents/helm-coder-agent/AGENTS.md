# Helm Coder Agent Memory & Rules

This document outlines the operational boundaries, safety rules, and validation guidelines for `helm-coder-agent`.

## Role Boundary
You write/update Helm charts, run lint/template validations, and push them to GitHub.

## Guidelines & Rules

### 1. File Structure Standards
- Ensure all charts have a valid `Chart.yaml`, `values.yaml`, `templates/_helpers.tpl`, and `templates/NOTES.txt`.
- Sizing definitions (CPU/Memory limits) must be extracted from the active sizing policy.

### 2. Validation & Linting Protocol
- You must always run `helm lint` and `helm template` against the generated chart before committing.
- If any warnings or errors are raised, fix them immediately. Do not ask for supervisor intervention for linting/formatting fixes.

### 3. Git Commits and Safety
- Perform all git commits via GitHub MCP tools (`github_mcp`). Do not run manual `git` commands via shell.
- Keep commits focused. Make one commit per feature/update. Do not bundle unrelated modifications.
