# Skills and memory

> Extend the agent with domain skills and persistent memory across sessions

k8s-autopilot uses **skills** and **memory** to build deep domain knowledge and remember what it learns:

- **Skills**: Modular instruction sets that give the agent domain expertise — Helm workflows, Prometheus alerting patterns, GitOps procedures, and more. Skills come from built-in sub-agents and installed plugins.
- **Memory**: `AGENTS.md` files that persist conventions and preferences across sessions, so the agent doesn't forget what you've taught it.

## Skills

Skills are the primary way k8s-autopilot gets its domain knowledge. Each skill is a directory containing a `SKILL.md` file with step-by-step procedures, reference patterns, and best practices for a specific operational area.

### Where skills come from

Since k8s-autopilot runs as a centralized server that clients connect to, skills are loaded from two sources:

| Source | How they work |
|---|---|
| **Built-in sub-agent skills** | Bundled with each operator — Helm, Kubernetes, observability (Prometheus, Loki, Tempo), app delivery (ArgoCD, Argo Rollouts), and more. These ship with k8s-autopilot and are always available. |
| **Plugin skills** | Installed from the marketplace or custom plugin sources via the Settings UI. These extend the agent with new capabilities for any domain. |

Built-in skills load automatically when their sub-agent is activated. Plugin skills become available as soon as the plugin is installed — no restart needed.

### How skills activate

Skills activate automatically based on your task. The agent matches skills to what you're asking based on their description: if you ask about Helm deployments, the Helm operator's skills load automatically; if you ask about Prometheus alerting, the observability skills activate. You don't need to configure or trigger anything manually.

### Two kinds of skills

- **Sub-agent skills**: Bundled with specific operators (Helm operator, Prometheus operator, etc.). These only load when that sub-agent is active — they don't consume tokens during general conversation. Each sub-agent's skills are scoped exclusively to that sub-agent.
- **Plugin skills**: Installed from marketplace plugins. If the plugin is an **agent plugin**, its skills are scoped to the plugin's sub-agent. If it's a **vertical plugin**, its skills bind directly to the main agent and are available in every conversation.

### Skill structure

A skill is a directory with a `SKILL.md` file:

```
my-skill/
├── SKILL.md          # Required — instructions with YAML frontmatter
├── scripts/          # Optional — helper scripts
├── references/       # Optional — domain-specific documentation and patterns
└── assets/           # Optional — templates, manifests, and examples
```

### SKILL.md format

```markdown
---
name: kubernetes-hardening
description: "Audit and enforce Pod Security Standards across Kubernetes namespaces"
tags: [security, kubernetes, compliance]
---

# Kubernetes Hardening Skill

When auditing or enforcing pod security:

1. Check all namespaces for PSA labels (enforce, audit, warn) at baseline or restricted level
2. Identify privileged containers and flag workloads running as root
3. Verify NetworkPolicies exist with default-deny ingress in each namespace
4. Check for missing resource limits and security contexts
5. Generate a compliance report with findings and recommended remediations
```

The YAML frontmatter provides the `name` and `description` that the agent uses for automatic skill matching.

### Installing skills via plugins

The primary way to add new skills to k8s-autopilot is through the plugin system:

1. Go to **Settings → Plugins** and connect a marketplace
2. Browse the **Discover** tab to find plugins with the skills you need
3. Click **Install** — the plugin's skills are immediately available to the agent

Each plugin card in the marketplace shows exactly which skills it includes, so you can see what capabilities you're adding before installing.

See [Plugins](./plugins.md) for the full walkthrough.

### Skill persistence

Installed plugin skills are persisted in the database so they survive container redeployments. If a skill file is missing from disk but exists in the database, k8s-autopilot automatically restores it. Built-in sub-agent skills are always part of the codebase and don't need this.

### Skill trust

To prevent unauthorized skills from running, k8s-autopilot tracks trust decisions for newly discovered skills. Built-in skills are automatically trusted. Skills from newly installed plugins are approved when you install the plugin from the UI.

## Memory

### What memory does

As you work with k8s-autopilot, it learns your preferences and conventions. When you tell it something like:

```
Remember that we always use ingress-nginx class "internal" for staging namespaces
```

It saves this to the `AGENTS.md` memory file and remembers it in future sessions — no need to repeat yourself.

Memory is where the agent stores things it learns during work: your preferred Helm chart sources, naming conventions, cluster topology patterns, and operational preferences.

### AGENTS.md

`AGENTS.md` is the primary memory file. It's loaded at the start of every session and injected into the agent's context so it follows your established conventions.

**Example `AGENTS.md`:**

```markdown
## Project Conventions

- All Helm releases must target the `platform` namespace unless explicitly specified
- Use `sealed-secrets` for all secret management — never commit plain Kubernetes Secrets
- Prometheus ServiceMonitors must include `release: kube-prometheus-stack` label
- All deployments require resource limits, liveness probes, and readiness probes

## Cluster Topology

- Staging: `eks-staging-us-east-1` (context: staging)
- Production: `eks-prod-us-east-1` (context: production)
- Always verify context before mutating operations
```

### What to store in memory

**Good for memory:**
- Team conventions and naming standards
- Cluster topology and environment mappings
- Preferred tools and chart repositories
- Architectural patterns and operational preferences
- Corrections you've given the agent (so it learns from mistakes)

**Not for memory:**
- API keys, tokens, or credentials (use the Settings UI for these)
- Transient diagnostic output or temporary troubleshooting notes
- One-off task-specific details that won't be relevant in future sessions

### Protected memory regions

Some sections of `AGENTS.md` are machine-managed — like your onboarding preferences. These are protected by marker comments and cannot be modified by the agent, even if it edits other parts of the file. If the agent accidentally changes a protected block, the system automatically restores it.

## Next steps

- **[Plugins](./plugins.md)** — Install skills and sub-agents from marketplaces
- **[Operators and Sub-agents](./operators-and-subagents.md)** — Which sub-agents use which skills
- **[Configuration](./configuration.md)** — Runtime settings and environment variables
