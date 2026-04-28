---
name: helm-generator
description: >-
  Generates complete, production-ready Helm chart files following Bitnami
  conventions. Use when the coordinator delegates chart generation for any
  application type. Reads the app-specific skill's SKILL.md and reference
  files to determine exactly which templates to create, then writes each
  file to the virtual workspace. Also use when asked to fix helm lint or
  helm template errors in previously generated charts. Triggers on keywords:
  Helm chart, generate chart, write templates, values.yaml, deployment.yaml,
  Chart.yaml, _helpers.tpl, scaffold chart, create chart, Bitnami.
compatibility: >-
  Requires write_file, read_file, edit_file, ls, grep tools operating on
  a virtual filesystem rooted at /workspace/. App-specific skills must be
  available under /skills/helm-operator/{app}-chart-generator/.
metadata:
  author: talkops-ai
  version: "2.0"
allowed-tools: read_file write_file edit_file ls grep
---

# Helm Chart Generator

## When to Use

Use this skill when the coordinator delegates chart file generation via
`task(helm-generator)`. This skill defines **how to write Go templates** —
the generic patterns and conventions. Each app's specific skill provides
**what to configure** — the data-level architecture specifications.

## Reference Files (Progressive Disclosure)

Load references **only when you reach the step that needs them**.

### App-Specific References (from `/skills/helm-operator/{app}-chart-generator/references/`)

| Reference | Contents | Read during |
|---|---|---|
| `execution-blueprint.md` | All K8s resources, configs, tradeoffs | Step 1 (always) |
| `values-schema.md` | Complete `values.yaml` structure with defaults | Step 3 |
| `scaling-and-resources.md` | HPA, PDB, scaling, monitoring per environment | Step 4 |
| `security-blueprint.md` | SecurityContext, NetworkPolicy, RBAC, SA | Step 4 |
| `manifest-patterns.md` | Expected rendered YAML per resource type | Cross-reference |

### Generic Template Patterns (from this skill's `references/`)

| Reference | Go template pattern for | Read during |
|---|---|---|
| `helpers-and-values.md` | `_helpers.tpl` boilerplate + `values.yaml` schema | Step 2 |
| `deployment-pattern.md` | Deployment / StatefulSet template | Step 4 |
| `service-pattern.md` | Service template | Step 4 |
| `autoscaling-pattern.md` | HorizontalPodAutoscaler template | Step 4 |
| `pdb-pattern.md` | PodDisruptionBudget template | Step 4 |
| `ingress-pattern.md` | Ingress / IngressRoute template | Step 4 |
| `networkpolicy-pattern.md` | NetworkPolicy template | Step 4 |
| `serviceaccount-pattern.md` | ServiceAccount template | Step 4 |
| `notes-pattern.md` | NOTES.txt post-install instructions | Step 5 |
| `readme-pattern.md` | Comprehensive README.md format | Step 5 |

## Chart Generation Workflow

Progress:
- [ ] Step 1: Discover app skill and read `execution-blueprint.md`
- [ ] Step 2: Write `_helpers.tpl` and `Chart.yaml`
- [ ] Step 3: Read `values-schema.md`, write `values.yaml`
- [ ] Step 4: Write all template files (core + auxiliary)
- [ ] Step 5: Write `NOTES.txt` and `README.md`
- [ ] Step 6: Self-validate and return summary

### Step 1. Discover and Load Architecture

1. Run `ls /skills/helm-operator/` to find the app-specific skill directory.
2. `read_file` the app-specific `SKILL.md` — it defines which templates to create.
3. `read_file` the app-specific `references/execution-blueprint.md` — it has all K8s resource specs.
4. Note the exact list of template files specified in the skill.

### Step 2. Helpers and Chart.yaml

1. Read `references/helpers-and-values.md` from THIS skill for the `_helpers.tpl` pattern.
2. Write `templates/_helpers.tpl` — **replace every `my-chart.` prefix with the actual chart name**.
3. Write `Chart.yaml` with name, version, appVersion, and description.

### Step 3. Values

1. Read the app-specific `references/values-schema.md` — it contains the complete `values.yaml`.
2. Write `values.yaml` copying that structure. Ensure every key referenced by templates has a default.

### Step 4. Templates

For each template file listed in the app-specific skill:

1. Read the relevant **generic pattern** from this skill's references (e.g., `deployment-pattern.md`)
2. Read any relevant **data reference** from the app's skill (e.g., `security-blueprint.md`)
3. Write the template, merging the generic pattern with app-specific configuration
4. **Replace every `my-chart.` prefix** in the pattern with the actual chart name

Write files to: `/workspace/helm-charts/{chart-name}/templates/{filename}`

**IMPORTANT**: Use ABSOLUTE paths starting with `/` for `write_file`.

### Step 5. NOTES.txt and README

1. Read `references/notes-pattern.md` for the NOTES.txt template.
2. Write `templates/NOTES.txt` with post-install instructions.
3. Read `references/readme-pattern.md` for the README documentation format.
4. Write `README.md` following the reference structure completely, ensuring the parameters table precisely matches the generated `values.yaml`.

### Step 6. Self-Validate

Before returning, verify:
1. Every `{{ .Values.X }}` reference in templates has a matching key in `values.yaml`
2. No hardcoded namespace — only `{{ .Release.Namespace }}`
3. All optional resources are guarded by `{{- if .Values.<resource>.enabled }}`
4. `_helpers.tpl` has: `<chart>.name`, `<chart>.fullname`, `<chart>.labels`, `<chart>.selectorLabels`, `<chart>.chart`, `<chart>.serviceAccountName`
5. No `my-chart.` prefixes remain in any file

If any check fails, fix the issue before returning.

## Safety Rules — MUST Follow

1. **Never hardcode namespaces.** All resources MUST use `{{ .Release.Namespace }}`. Exception: the `namespace.yaml` template itself.

2. **All optional resources require `.enabled` toggles.** Wrap each optional template (Ingress, HPA, PDB, NetworkPolicy, ServiceAccount) in `{{- if .Values.<resource>.enabled }}`.

3. **Image tag must default to `.Chart.AppVersion`.** Use `{{ .Values.image.tag | default .Chart.AppVersion }}`. Never hardcode image tags.

4. **Resource limits are mandatory.** Every container MUST have `resources.requests` and `resources.limits`. Use `{{- toYaml .Values.resources | nindent 12 }}`.

5. **Security context is mandatory.** Every pod MUST include `securityContext` with `runAsNonRoot: true` and `capabilities.drop: [ALL]` per Bitnami convention.

6. **Liveness and readiness probes are mandatory.** Every long-running container MUST have both probes via `{{- toYaml .Values.livenessProbe | nindent 12 }}`.

7. **Do not create files not listed in the skill.** Only generate the template files explicitly declared in the app-specific SKILL.md. Do not add extra resources the skill didn't request.

## Gotchas

- The `_helpers.tpl` name prefix MUST exactly match the chart name. If the chart is `my-app`, helpers must be `my-app.fullname`, NOT `my-chart.fullname`. This is the #1 cause of silent template failures.

- When `autoscaling.enabled` is `true`, the Deployment template **MUST NOT** set `.spec.replicas`. The `{{- if not .Values.autoscaling.enabled }}` guard is required. Without it, HPA and static replicas fight.

- `write_file` uses VIRTUAL paths starting with `/`. Do NOT use `./` relative paths.

- If `write_file` fails with a path error THREE times, STOP and return: "FAILED: Unable to write files to /workspace/helm-charts/{chart}/ after 3 attempts."

- Always write `values.yaml` BEFORE templates. Templates reference Values keys that must exist.

## Response Format

Return a manifest summary:
```
Generated {N} files for {app} Helm Chart:
  - Chart.yaml: chart metadata (v{version})
  - values.yaml: {M} configurable parameters
  - templates/_helpers.tpl: 6 named templates
  - templates/deployment.yaml: consumes Values.image, Values.resources, ...
  - templates/service.yaml: consumes Values.service
  - ...
  - templates/NOTES.txt: post-install instructions
  - README.md: usage and values reference

Key design decisions: [brief summary of architecture choices]
```
