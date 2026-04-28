---
name: helm-skill-builder
description: >-
  Generates per-application skill directories under /skills/ with SKILL.md
  and reference files that guide the helm-generator sub-agent. Use when the
  coordinator determines no skill exists for the requested application type
  AND the helm-planner did not auto-generate skills. This is the fallback
  skill builder for generic chart generation requests. Triggers on keywords:
  skill builder, create skill, scaffold skill, new app type, unknown chart.
compatibility: >-
  Requires write_file, read_file, ls tools. Virtual filesystem rooted at /.
  Must write to /skills/helm-operator/{app}-chart-generator/.
metadata:
  author: talkops-ai
  version: "2.0"
allowed-tools: read_file write_file ls
---

# Helm Skill Builder

## When to Use

Use this skill as a **fallback** when:
1. No app-specific skill exists under `/skills/helm-operator/{app}-chart-generator/`
2. The helm-planner did NOT auto-generate skills (its output does not contain "Skills written for")

If the planner already wrote skills, this agent is NOT needed — the coordinator skips it.

## Workflow

Progress:
- [ ] Step 1: Read conventions from memory
- [ ] Step 2: Determine the template file set
- [ ] Step 3: Write SKILL.md
- [ ] Step 4: Write reference files
- [ ] Step 5: Return summary

### Step 1. Read Conventions

```
read_file /memory/helm-operator/AGENTS.md
```
Check for any global conventions (naming, labels, security requirements).

### Step 2. Determine Template File Set

Based on the application type, determine which templates are needed:

**Always create:**
- `Chart.yaml`, `values.yaml`
- `templates/_helpers.tpl`, `templates/deployment.yaml`, `templates/service.yaml`

**Add based on app needs:**
- `templates/ingress.yaml` — when the app exposes HTTP endpoints
- `templates/hpa.yaml` — when autoscaling is relevant
- `templates/pdb.yaml` — for HA deployments (replicaCount > 1)
- `templates/configmap.yaml` — when the app has config files
- `templates/secret.yaml` — when the app has sensitive config
- `templates/serviceaccount.yaml` — when RBAC is needed
- `templates/networkpolicy.yaml` — when network isolation is needed
- `templates/NOTES.txt` — always recommended

### Step 3. Write SKILL.md

Write to `/skills/helm-operator/{app}-chart-generator/SKILL.md` with this structure:

```yaml
---
name: {app}-chart-generator
description: >-
  Generates production-grade Helm chart for {App}. Use when asked to
  create, scaffold, or deploy {App} on Kubernetes. [Add specific keywords].
compatibility: >-
  Requires write_file, read_file, edit_file, ls, grep tools.
  Virtual filesystem rooted at /workspace/.
metadata:
  author: helm-skill-builder
  version: "1.0"
allowed-tools: write_file read_file edit_file ls grep
---
```

Body MUST include:
- **When to Use** section
- **Reference Files** table (progressive disclosure)
- **Chart Generation Workflow** with step checklist
- **Safety Rules** (≥5 rules)
- **Gotchas** (≥3 items)
- **Response Format**

Follow the patterns from existing skills under `/skills/helm-operator/`.

### Step 4. Write Reference Files

Create these reference files under `/skills/helm-operator/{app}-chart-generator/references/`:

| File | Contents |
|---|---|
| `execution-blueprint.md` | Kubernetes resource specifications (ports, probes, env vars, volumes) |
| `values-schema.md` | Complete `values.yaml` structure with defaults for all parameters |
| `scaling-and-resources.md` | Resource requests/limits, HPA targets, PDB config |
| `security-blueprint.md` | SecurityContext, NetworkPolicy rules, RBAC |

### Step 5. Return Summary

```
Skill written at /skills/helm-operator/{app}-chart-generator/.
Declared file set: [list of template files the generator should create]
```

## Safety Rules — MUST Follow

1. **SKILL.md must follow agentskills.io specification.** Name must be lowercase a-z and hyphens only, 1-64 chars, matching the directory name.

2. **Description must include "Use when" triggers.** The description field is used for skill routing — vague descriptions cause misrouting.

3. **Never hardcode app-specific values in SKILL.md body.** Put data in reference files. SKILL.md is the procedure, references are the data.

4. **Reference files must have concrete defaults.** Every value in `values-schema.md` must have a sensible production default, not TODO placeholders.

## Gotchas

- The skill directory name MUST match the `name` field in SKILL.md frontmatter exactly. Mismatches cause the skill loader to skip the skill silently.

- Don't over-specify templates for simple apps. A basic web app needs: deployment, service, ingress, _helpers.tpl. Adding HPA/PDB/NetworkPolicy for a dev-only tool wastes tokens.

- The helm-generator sub-agent reads the SKILL.md first, then loads references on demand. Keep SKILL.md under 5000 tokens and put detailed specs in references.

## Response Format

```
Skill written at /skills/helm-operator/{app}-chart-generator/.
Declared file set: [Chart.yaml, values.yaml, templates/_helpers.tpl, templates/deployment.yaml, ...]
Reference files: [execution-blueprint.md, values-schema.md, ...]
```
