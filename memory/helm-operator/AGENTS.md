# Helm Operator Agent Memory

## Core Directives
1. Never assume deletions are safe — always get HITL approval for destructive ops.
2. Never ignore validators — surface exact errors, never silently suppress.
3. Calls to `request_user_input` MUST include `options` (action buttons). Calls without options are rejected.

## Mandatory Gates — request_user_input Patterns

### §1 Commit Gate — after validation passes
| Arg | Value |
|-----|-------|
| title | `"Chart '{app}' Validated ✅"` |
| question | Summary of files generated and path |
| context | `"Would you like to push to GitHub or keep locally?"` |
| options | `[{"key":"push_to_github","label":"🚀 Push to GitHub","primary":true}, {"key":"keep_local","label":"📁 Keep Local"}]` |
| input_fields | `[{"key":"repository","label":"Repository (owner/repo)"}, {"key":"branch","label":"Branch","default":"main"}]` |

### §2 Next Steps Gate — after commit decision
| Arg | Value |
|-----|-------|
| title | `"What's Next?"` |
| question | Summary of what was accomplished |
| options | `[{"key":"generate_another","label":"➕ Generate Another Chart","primary":true}, {"key":"deploy_chart","label":"🚢 Deploy to Cluster"}, {"key":"done","label":"✅ I'm Done"}]` |
| input_fields | `[{"key":"details","label":"Details for next action"}]` |

### §4 Helm Operation Gate — after any runtime helm-operation
| Arg | Value |
|-----|-------|
| title | `"Helm Operation Complete"` |
| question | Summary of what was returned or accomplished |
| options | `[{"key":"continue_helm","label":"🔧 Another Helm Operation","primary":true}, {"key":"done","label":"✅ I'm Done"}]` |
| input_fields | `[{"key":"details","label":"Describe your next action (optional)"}]` |

### §3 Plan Review Gate — before helm operations
| Arg | Value |
|-----|-------|
| title | `"Deployment Plan Review"` |
| question | Deployment plan details |
| options | `[{"key":"approve","label":"✅ Approve","primary":true}, {"key":"modify","label":"✏️ Modify"}, {"key":"reject","label":"❌ Cancel"}]` |
| input_fields | `[{"key":"modifications","label":"Modifications (if any)"}]` |

## Destructive Ops — require HITL approval via interrupt_on
- `helm_uninstall_release`, `helm_rollback_release`, `helm_upgrade_release`
- Explain blast radius → present tool input → pause via interrupt_on

## Safe Ops — no gate required
- Read-only: helm-discovery, get/list tools
- Local: writing, patching, validating templates locally

## Operations Journal — Context Persistence
After every state-modifying helm operation (install, upgrade, rollback, uninstall),
the coordinator MUST call `log_helm_operation` to persist context to
`/memories/helm-operator/operations-log.md`.

### Journal Entry Fields
| Field | Description |
|-------|-------------|
| action | install / upgrade / rollback / uninstall |
| release_name | The helm release name |
| namespace | Target namespace |
| chart_source | Full chart reference (e.g., oci://..., bitnami/nginx) |
| values | Key values that were set (e.g., replicaCount=2) |
| version | Chart version |
| notes | Optional context |

### Context Recovery Rules
1. The `OperationContextMiddleware` auto-injects recent operations before every model call.
2. For follow-up operations (upgrade, modify values), the coordinator MUST include
   full chart source, release name, namespace, and previous values in the task description.
3. The helm-operation subagent MUST check the task description and operations journal
   BEFORE asking the user for any parameter. User input is a LAST RESORT.
4. For upgrades that only change values: use `--reuse-values` flag. The original
   chart URL is NOT needed for simple value changes on existing releases.

