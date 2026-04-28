---
name: traefik-edge-routing
description: >-
  Use when the user asks to create or update routes, split traffic, run canary
  deployments with weighted routing, set up middleware (rate limiting, circuit
  breakers, auth, IP allowlists), mirror traffic for shadow testing, migrate
  from NGINX to Traefik, configure TCP routing, manage TLS, enable sticky
  sessions, or monitor traffic health — even if they don't mention Traefik by
  name. Triggers on keywords: Traefik, IngressRoute, TraefikService, canary
  route, traffic split, weighted routing, middleware, rate limit, circuit
  breaker, NGINX migration, shadow launch, traffic mirroring, TCP route,
  sticky session, strip prefix, forward auth, ServersTransport, traffic anomaly.
metadata:
  author: talkops.ai
  version: '3.0'
  mcp_server: Traefik MCP Server
compatibility: >-
  Requires Traefik MCP Server (FastMCP-based, Docker or Python 3.10+).
  Kubernetes cluster with Traefik Ingress Controller and valid kubeconfig.
---

# Traefik Edge Routing Skill

## When to Use

Load this skill for any **state-modifying** Traefik operation: creating routes, splitting
canary traffic, attaching middlewares, shadow-testing new versions, migrating from NGINX,
configuring TCP routes, enabling sticky sessions.

Read-only queries (list routes, check traffic distribution, view metrics, check anomalies)
do NOT need this skill — the sub-agent handles those directly via the Observability
Fast-Path without loading any files.

## MCP Server Context

All tools are provided by the **Traefik MCP Server** (server name: `Traefik`).

**Prerequisites:** Kubernetes cluster with Traefik Ingress Controller installed. Valid `KUBECONFIG` mounted.

**Write-gate:** Set `MCP_ALLOW_WRITE=false` to block cluster mutations — YAML generation via `action=generate` still works in read-only mode.

**generate-before-apply pattern:** For migrations and new route creation, always default to `action=generate` first to produce reviewable YAML, then re-execute with `action=apply` or `action=create` after user confirmation.

## MCP Resources

Resource URIs are listed in the system prompt's Observability Fast-Path table — do not duplicate here.
For resource composition patterns during multi-step workflows, see `references/workflows.md`.

## Core Workflow: Explore → Plan → Implement → Verify

### 1. Explore
- Identify current state using MCP resources (see system prompt's resource table).
- Check the operations journal (`/memories/app-operator/operations-log.md`) for recent context.
- Verify target resources exist before attempting create (idempotency).

### 2. Plan
- Present a clear action plan to the user with before/after state.
- For weight changes: show current vs proposed weights.
- For migrations: show generated YAML and breaking annotations.
- For middleware: show configuration and which routes are affected.
- Use `request_human_input` for approval on state-modifying operations.

### 3. Implement
- Execute the approved plan using MCP tools.
- Follow the generate-before-apply pattern for migrations and manifests.
- Tools are additionally gated by `HumanInTheLoopMiddleware`.

### 4. Verify
- **Weight changes**: Read distribution → check Prometheus → check anomalies.
- **Middleware**: Verify attachment via route distribution.
- **Migration**: Verify converted routes via migration status.
- Never declare success based solely on tool stdout.

## Tool Reference

### Edge Routing & Traffic Splitting (2 tools)

| Tool | Actions | Purpose |
|------|---------|---------|
| `traefik_manage_weighted_routing` | `create` \| `update` \| `delete` | Weighted canary routes — creates TraefikService (WRR) + IngressRoute pair |
| `traefik_manage_simple_route` | `create` (upsert) \| `delete` | Direct IngressRoute to a K8s Service, no weight splitting |

**When to use which routing tool:**
- Canary/progressive delivery → `traefik_manage_weighted_routing`
- Standard direct routing (no split) → `traefik_manage_simple_route`

**Key parameters for `traefik_manage_weighted_routing`:**

| Parameter | Purpose | Example |
|-----------|---------|---------|
| `path_prefix` | Scope to URL path | `/api` |
| `path_match_type` | `PathPrefix` \| `Path` \| `PathRegexp` | `PathPrefix` |
| `header_name` / `header_value` | Header-based routing (create only) | `X-Canary: true` |
| `cookie_name` / `cookie_regex` | Cookie-based routing (create only) | `canary`, `.*yes.*` |
| `tls_enabled` + `tls_secret_name` | TLS termination | `api-tls` |
| `middlewares` | Middleware names to attach on create | `["rate-limit", "auth"]` |

### Middleware & Resiliency (2 tools)

| Tool | Actions | Supported Types |
|------|---------|----------------|
| `traefik_manage_middleware` | `create` (upsert) \| `update` \| `delete` | `rate_limit`, `circuit_breaker`, `strip_prefix`, `redirect_scheme`, `inflight_req`, `headers`, `ip_allowlist`, `ip_denylist`, `forward_auth`, `buffering`, `replace_path`, `replace_path_regex`, `add_prefix` |
| `traefik_manage_route_middlewares` | `attach` \| `detach` | Add/remove middlewares on a live IngressRoute |

### Traffic Mirroring (1 tool)

| Tool | Actions | Purpose |
|------|---------|---------|
| `traefik_manage_traffic_mirroring` | `enable` \| `update` \| `disable` | Shadow-copy a % of production traffic to canary; users always get stable responses |

### Backend Transport & Affinity (2 tools)

| Tool | Actions | Purpose |
|------|---------|---------|
| `traefik_manage_servers_transport` | `create` \| `delete` | ServersTransport CRD — backend dial/response timeouts, backend TLS config |
| `traefik_configure_service_affinity` | `enable` \| `disable` | Sticky-cookie annotations on K8s Services |

### TCP Routing (2 tools)

| Tool | Actions | Purpose |
|------|---------|---------|
| `traefik_manage_tcp_routing` | `create` \| `delete` | IngressRouteTCP for PostgreSQL, Redis, MQTT, or any TCP protocol |
| `traefik_configure_tcp_middleware` | `create` | TCP IP allowlist MiddlewareTCP |

### NGINX Migration (1 tool)

| Tool | Actions | Purpose |
|------|---------|---------|
| `traefik_nginx_migration` | `generate` \| `apply` \| `revert` | NGINX Ingress → Traefik CRD translation; supports agentic overrides for breaking annotations |

**Common NGINX annotation mapping (quick reference):**

| NGINX Annotation | Traefik Middleware | Type |
|---|---|---|
| `nginx.ingress.kubernetes.io/limit-rps` | `rate_limit` (average=[value], period=1s) | `rate_limit` |
| `nginx.ingress.kubernetes.io/whitelist-source-range` | `ip_allowlist` (source_range=[value]) | `ip_allowlist` |
| `nginx.ingress.kubernetes.io/auth-url` | `forward_auth` (address=[value]) | `forward_auth` |
| `nginx.ingress.kubernetes.io/ssl-redirect` | `redirect_scheme` (scheme=https) | `redirect_scheme` |
| `nginx.ingress.kubernetes.io/proxy-body-size` | `buffering` (maxRequestBodyBytes=[value]) | `buffering` |

### Manifest Generator (1 tool)

| Tool | `manifest_type` values | Purpose |
|------|----------------------|---------|
| `traefik_generate_routing_manifest` | `traefik_service` \| `ingress_for_traefik_service` \| `ingress_for_services` \| `mirroring` \| `ingress_route_tcp` \| `middleware_tcp` | Generate any Traefik YAML for GitOps review |

## Workflow Routing

Load `references/workflows.md` ONLY when executing a multi-step workflow from the table below.
Read ONLY the section matching the selected workflow — do NOT load the entire file.

| User Intent | Workflow Section |
|-------------|---------|
| Create a new weighted canary route | `#1-weighted-canary-route--create` |
| Progressively shift canary traffic | `#2-progressive-traffic-shift` |
| Route by HTTP header or cookie | `#3-header--cookie-canary-routing` |
| Shadow-test a new version silently | `#4-shadow-launch-traffic-mirroring` |
| Add/update middleware on a route | `#5-middleware-management` |
| Migrate from NGINX to Traefik | `#6-nginx-migration` |
| Route TCP protocol (Postgres, Redis, MQTT) | `#7-tcp-routing` |
| Set up TLS termination on a route | `#8-tls-termination-setup` |
| Configure sticky sessions | `#9-sticky-sessions` |
| Investigate traffic anomaly or error spike | `#10-traffic-investigation` |

## Safety Rules — MUST Follow

1. **Read distribution before updating weights.** Always read `traefik://traffic/{ns}/{route}/distribution` before calling `traefik_manage_weighted_routing(action='update')`. Confirm the route exists and note current weights to avoid unintended zeroing.

2. **Generate before apply for migrations.** For `traefik_nginx_migration`, always call `action=generate` first. Show the bundle output to the user, flag any detected breaking annotations, then re-call with `action=apply` only on confirmation.

3. **Check migration compatibility before scanning.** Run `traefik://migration/nginx-ingress-analyze` before any NGINX migration to surface breaking annotations. If breaking annotations exist, prepare a `migration_plan` override before applying.

4. **Never set both canary weight to 0 and stable to 0.** A `0/0` weight state makes the route unreachable. When deleting a canary route, use `action=delete` — do not zero both weights.

5. **Traffic mirroring is zero user impact — but not zero cluster impact.** Mirrored traffic hits the canary service and consumes resources. Advise users to size the canary service before enabling high mirror percentages (>50%).

6. **Confirm destructive actions.** Confirm before: `action=delete` on any route or middleware, `action=revert` on migrations, `traefik_configure_service_affinity(action='disable')`. State what traffic will be affected.

7. **Never hardcode credentials in middleware.** `forward_auth` and `headers` middlewares that require secrets must reference Kubernetes Secrets — never inline credentials in tool parameters.

8. **Monitor after weight shifts.** After every `update` to traffic weights, first verify `traefik://metrics/prometheus/status` is connected, then read `traefik://metrics/{ns}/{svc}/summary` and `traefik://anomalies/detected` to confirm error rates are stable.

9. **TCP routes have no health rollback.** Unlike HTTP canary routes, TCP IngressRouteTCP has no weight-based rollback. Confirm TCP service availability before creating routes.

10. **Always define sourceCriterion for rate limits.** When creating `rate_limit` middleware, always include a `sourceCriterion` (e.g., `ipStrategy` or `requestHeaderName`). Without it, the rate limit applies globally across all clients — one heavy user exhausts the budget for everyone.

11. **ACME challenge interception on TLS passthrough.** In Traefik v3, routers with TLS passthrough may have ACME challenges intercepted by Traefik, breaking downstream certificate management. If ACME cert issuance fails: (a) Check Traefik logs for "Cannot retrieve the ACME challenge" errors. (b) Recommend `allowACMEByPass: true` on the affected Entrypoint. (c) Ensure ACME-specific routers have explicit `priority` higher than generic HTTP→HTTPS redirect routers.

12. **Idempotency check before create.** Before creating any resource, check if it already exists (see Idempotency Rules in system prompt). Use `traefik://traffic/{ns}/{route}/distribution` to verify — if the resource exists, use `action='update'` instead of `action='create'`.

13. **Cross-domain coordination with Argo Rollouts.** When Traefik routes target services also managed by Argo Rollouts canary, use Traefik for L7 edge splitting and Argo Rollouts for pod-level delivery. Do NOT use both on the same service without explicit user confirmation — weight conflicts between Traefik WRR and Rollouts canary steps will cause unpredictable traffic distribution.

## Gotchas

- **`action=update` cannot change header/cookie match rules.** Match conditions are set on
  `action=create` only. To change them, delete the route and recreate with new match rules.
- **Middleware names are namespace-scoped with provider suffix.** IngressRoute annotations
  reference middleware as `middleware-name@kubernetescrd`. Omitting `@kubernetescrd` causes
  silent 404 errors — Traefik doesn't report the missing middleware.
- **Rate limit `average` is per-second by default.** If user says "100 requests per minute",
  set `average: 100, period: "60s"` — NOT `average: 100, period: "1s"`.
- **TraefikService WRR with 0 ready pods returns 503.** Traefik doesn't skip empty backends
  in weighted round-robin. The canary service MUST have at least 1 ready pod before shifting
  any traffic to it, or the route will serve 503s proportional to canary weight.
- **Circuit breaker expression is case-sensitive.** `NetworkErrorRatio()` works;
  `networkerrorratio()` silently fails without error. Always use exact PascalCase.
- **IngressRoute `priority` defaults to rule length.** Longer `match` rules get higher
  priority. A short catch-all `Host(\`example.com\`)` may shadow a longer
  `Host(\`example.com\`) && PathPrefix(\`/api\`)` if the catch-all was created first.

## Response Format

- For weight changes: report before/after — "Shifted from 90/10 → 80/20 (stable/canary)."
- For middleware operations: confirm which route it's attached to and current middleware chain.
- For migration: present a compatibility table (supported / unsupported / breaking) before asking to proceed.
- For anomalies: report anomaly type + affected route + recommended action.
- For generated YAML (`action=generate`): render in a code block with explicit "Review and confirm to apply" prompt.
- For circuit breaker configuration: report expression used + current threshold + available expressions (`NetworkErrorRatio()`, `ResponseCodeRatio(min, max, minOK, maxOK)`, `LatencyAtQuantileMS(quantile)`).
