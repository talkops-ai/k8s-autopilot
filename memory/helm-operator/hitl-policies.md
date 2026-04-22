# Helm Operator — HITL Policies (Extended Reference)

> **Note:** The critical gate patterns (§1–§3) are in AGENTS.md which is
> always loaded. This file provides additional detail for edge cases.

## Commit Gate — Additional Rules

- This gate MUST fire even if the user's original request did not mention GitHub.
- The default outcome is **Keep Local** — never assume GitHub push.
- Do NOT return a text summary instead of calling `request_user_input`.
- Do NOT skip this gate because "the user only asked to generate a chart."
- Do NOT end the conversation before this gate has been triggered.

## Next Steps Gate — Additional Rules

- This gate MUST fire regardless of the Commit Gate outcome.
- Do NOT end the conversation after the Commit Gate without triggering this gate.
- Do NOT assume the user is done after a single chart generation.

## Destructive Operations — Detailed Policy

The following operations are **destructive or high-risk** and require
explicit `approve` or `reject` from a human:

| Operation | Risk Level | Gate Type |
|-----------|-----------|-----------|
| `helm_uninstall_release` | Critical — data loss possible | interrupt_on |
| `helm_rollback_release` | High — service disruption | interrupt_on |
| `helm_upgrade_release` | High — config drift | interrupt_on |

### Execution Protocol
1. Explain the blast radius of the change.
2. Present the exact tool input you intend to use.
3. Pause execution by calling the relevant tool where `interrupt_on` config is set.

## Safe Operations (No Gate Required)

- **Read-Only**: `helm-discovery` (and any associated get/list tools).
- **Local File Operations**: Writing, patching, or validating Helm templates locally.
- **Fresh Installs**: `helm_install_chart` is deemed safe *unless* configured to pause by an admin.
