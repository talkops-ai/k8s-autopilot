# Helm Operator — Shared Agent Memory

## Core Directives
1. Never assume deletions are safe — always get HITL approval for destructive ops.
2. Never ignore validators — surface exact errors, never silently suppress.
3. Calls to `request_user_input` MUST include `options` (action buttons). Calls without options are rejected.

## Context Recovery Rules
1. For follow-up operations (upgrade, modify values), include full chart source,
   release name, namespace, and previous values in the task description.
2. Check the task description BEFORE asking the user for
   any parameter. User input is a LAST RESORT.
3. For upgrades that only change values: use `--reuse-values` flag. The original
   chart URL is NOT needed for simple value changes on existing releases.

## Shared Scratchpad — Cross-Domain Collaboration
The `/shared/` directory is a cross-domain workspace visible to ALL coordinators.
Use it to persist information that other domains might need.

### Write Rules
- Write ONLY distilled findings, never raw helm output or full values dumps.
- Use the naming convention: `/shared/helm/{topic}.md`
- Always include a `## Context` header with who wrote it and when.
- Keep each file under 500 words.

### Read Rules
- At the START of any operation, check `/shared/` for relevant cross-domain context.
- If the Observability agent has written alert data about a service, USE it when deciding upgrade parameters.
- If cross-domain context exists in `runtime.context.cross_domain_context`, prefer it over `/shared/` files.

### What This Domain Writes
| Trigger | Path | Content |
|---|---|---|
| Release installed/upgraded | `/shared/helm/release-{name}.md` | Release name, namespace, chart, version, key values |
| Chart generated | `/shared/helm/chart-{name}.md` | Chart name, templates, dependencies |

### What This Domain Reads
| Path | Written By | Use Case |
|---|---|---|
| `/shared/observability/triage-context.md` | Observability | Alert context for services being upgraded |
| `/shared/k8s/pod-status-{service}.md` | K8s Operator | Pod health for services being modified |
| `/shared/app/deployment-{name}.md` | App Operator | ArgoCD sync state for managed releases |

## Destructive Ops — require HITL approval via interrupt_on
- `helm_uninstall_release`, `helm_rollback_release`, `helm_upgrade_release`
- Explain blast radius → present tool input → pause via interrupt_on

## Safe Ops — no gate required
- Read-only: helm-discovery, get/list tools
- Local: writing, patching, validating templates locally
