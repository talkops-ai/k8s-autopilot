# Traefik Workflows Reference

Detailed step sequences for all edge routing operations. Load this file when executing any multi-step Traefik workflow.

## Table of Contents
1. [Weighted Canary Route — Create](#1-weighted-canary-route--create)
2. [Progressive Traffic Shift](#2-progressive-traffic-shift)
3. [Header / Cookie Canary Routing](#3-header--cookie-canary-routing)
4. [Shadow Launch (Traffic Mirroring)](#4-shadow-launch-traffic-mirroring)
5. [Middleware Management](#5-middleware-management)
6. [NGINX Migration](#6-nginx-migration)
7. [TCP Routing](#7-tcp-routing)
8. [TLS Termination Setup](#8-tls-termination-setup)
9. [Sticky Sessions](#9-sticky-sessions)
10. [Traffic Investigation](#10-traffic-investigation)

---

## 1. Weighted Canary Route — Create

**Trigger phrases:** "create canary route", "set up weighted routing", "create TraefikService for canary"

```
Step 1 → Read: traefik://traffic/routes/list
         — Confirm no existing route with the same name (upsert-safe, but verify intent)

Step 2 → Traefik:traefik_manage_weighted_routing (action='create')
         Required params:
           - route_name:     name of the TraefikService + IngressRoute pair
           - namespace:      target namespace
           - hostname:       public hostname (e.g., api.example.com)
           - stable_service: name of the stable K8s Service
           - canary_service: name of the canary K8s Service
           - stable_weight:  e.g., 100 (start at 100% stable, 0% canary)
           - canary_weight:  e.g., 0
         Optional:
           - path_prefix / path_match_type  (scope to URL path)
           - tls_enabled + tls_secret_name  (enable TLS — see Workflow 8)
           - middlewares: ["rate-limit", "auth"]  (attach on creation)

         Creates: TraefikService (weighted round-robin) + IngressRoute pointing to it

Step 3 → Read: traefik://traffic/{ns}/{route_name}/distribution
         — Confirm route is live with correct weights, entrypoints, match rules

Step 4 → Read: traefik://metrics/{ns}/{stable_service}/summary
         — Confirm traffic is flowing without error spikes
```

**Start at 100/0, never 0/0.** Route is live immediately on create. Always start with full stable traffic until canary is validated.

---

## 2. Progressive Traffic Shift

**Trigger phrases:** "shift traffic to canary", "increase canary weight", "progressive rollout with Traefik", "promote canary traffic"

```
Pre-flight:
→ Read: traefik://traffic/{ns}/{route}/distribution    — capture current weights
→ Read: traefik://anomalies/detected                   — confirm no active anomalies

At each shift step:
→ Traefik:traefik_manage_weighted_routing (action='update')
  stable_weight + canary_weight must sum to 100
→ Read: traefik://metrics/{ns}/{svc}/summary            — check error rate + P99 latency
→ Read: traefik://anomalies/detected                    — check for new anomalies
→ Decision:
    Metrics green → continue to next step
    Metrics red   → revert: update weights back to previous split

Standard progression (adjust per use case):
  100/0 → 95/5 → 90/10 → 75/25 → 50/50 → 0/100
  (pause at each step; validate metrics before advancing)

Final promotion (canary becomes new stable):
→ Traefik:traefik_manage_weighted_routing (action='update', stable_weight=0, canary_weight=100)
→ Redeploy stable service with new image
→ Traefik:traefik_manage_weighted_routing (action='update', stable_weight=100, canary_weight=0)
  — Now pointing 100% to updated stable; canary service can be scaled down
→ Traefik:traefik_manage_weighted_routing (action='delete')   — clean up canary route
→ Traefik:traefik_manage_simple_route (action='create')       — replace with simple direct route
```

**Emergency revert:** Set `stable_weight=100, canary_weight=0` immediately — takes effect in milliseconds.

---

## 3. Header / Cookie Canary Routing

**Trigger phrases:** "header-based canary", "cookie canary", "route by header", "targeted canary", "internal testing canary"

**Use case:** Route a specific user segment (e.g., internal testers, beta users) to the canary version without shifting percentage traffic. Header/cookie rules are set on create and cannot be changed via update — delete and recreate to change match conditions.

```
Step 1 → Traefik:traefik_manage_weighted_routing (action='create')
         — With header routing:
           header_name: "X-Canary"
           header_value: "true"
           stable_weight: 0, canary_weight: 100
           (All requests with header X-Canary: true → canary service)

         — With cookie routing:
           cookie_name: "canary"
           cookie_regex: ".*yes.*"
           (Requests with cookie canary matching regex → canary service)

Step 2 → Read: traefik://traffic/{ns}/{route}/distribution
         — Confirm match rules are applied correctly

Step 3 → Test: send a request with the target header/cookie
         — Verify it reaches canary service

Step 4 → (When done) Traefik:traefik_manage_weighted_routing (action='delete')
         — Remove header/cookie route after testing
```

**Header vs. cookie choice:**
- Header-based → for internal tools, CI pipelines, Postman tests
- Cookie-based → for browser-based beta programs where a cookie is set on login

---

## 4. Shadow Launch (Traffic Mirroring)

**Trigger phrases:** "shadow launch", "mirror traffic", "silent canary", "shadow test", "zero user impact testing"

**What it does:** Copies a % of real production requests to the canary service. The stable service always responds to users — canary responses are silently discarded. Canary still processes real traffic and logs/metrics are generated.

```
Step 1 → Read: traefik://traffic/{ns}/{route}/distribution
         — Confirm the canary route exists; note stable service name

Step 2 [Pre-flight] → Confirm canary service is deployed and scaled appropriately
         — Mirrored traffic consumes real resources on the canary

Step 3 → Traefik:traefik_manage_traffic_mirroring (action='enable')
         Required params:
           - route_name:    existing weighted route name
           - namespace:     target namespace
           - mirror_percent: start low (10–20%)
         Effect: Traefik copies mirror_percent of requests to canary; stable still responds

Step 4 → Monitor canary behaviour:
         → Read: traefik://metrics/{ns}/{canary_service}/summary
           — Compare: error rate, latency vs. stable baseline
         → Read: traefik://anomalies/detected
           — Check for canary-side errors

Step 5a [Canary healthy] → Ramp mirroring:
         → Traefik:traefik_manage_traffic_mirroring (action='update', mirror_percent=50)
         → Continue monitoring; ramp to 100% if desired
         → Proceed to progressive traffic shift (Workflow 2) to promote

Step 5b [Canary failing] → Disable immediately:
         → Traefik:traefik_manage_traffic_mirroring (action='disable')
         — Zero user impact: stable was always responding

Step 6 → Traefik:traefik_manage_traffic_mirroring (action='disable')
         — Always disable after shadow testing is complete; canary resources can be freed
```

**Shadow vs. weighted canary:**
- Shadow → canary processes traffic but users see only stable (safest for untested versions)
- Weighted canary → real users are split; some actually receive canary responses (use after shadow validation)

---

## 5. Middleware Management

**Trigger phrases:** "add rate limit", "circuit breaker", "strip prefix", "attach middleware", "IP allowlist", "forward auth", "detach middleware"

### Creating Middleware

```
Traefik:traefik_manage_middleware (action='create')
  Required: middleware_name, namespace, middleware_type
  Type-specific params (examples):

  rate_limit:
    average: 100   (requests per period)
    period: "1s"
    burst: 200

  circuit_breaker:
    expression: "NetworkErrorRatio() > 0.3"
    response_code: 503   (optional)

  strip_prefix:
    prefixes: ["/api"]

  ip_allowlist:
    source_range: ["10.0.0.0/8", "192.168.1.0/24"]

  forward_auth:
    address: "http://auth-service.default.svc.cluster.local/auth"
    (Never inline credentials — reference K8s Secrets)

  headers:
    custom_request_headers: {"X-Frame-Options": "DENY"}
    custom_response_headers: {"X-Content-Type-Options": "nosniff"}
```

### Attaching / Detaching Middleware

```
Attach:
→ Traefik:traefik_manage_route_middlewares (action='attach')
  params: route_name, namespace, middleware_name
→ Read: traefik://traffic/{ns}/{route}/distribution
  — Confirm middleware appears in the middleware chain

Detach:
→ Traefik:traefik_manage_route_middlewares (action='detach')
  params: route_name, namespace, middleware_name
→ Confirm detached — middleware CRD still exists (reattach later if needed)
```

### Middleware Ordering (important)
Middleware runs in the order listed on the IngressRoute. Recommended order for most routes:
1. `ip_allowlist` (reject early)
2. `rate_limit` (before auth to reduce load)
3. `forward_auth`
4. `strip_prefix` / `replace_path` (path manipulation)
5. `headers` (response decoration)

---

## 6. NGINX Migration

**Trigger phrases:** "migrate from NGINX", "NGINX to Traefik", "convert Ingress to IngressRoute", "NGINX migration"

```
Phase 1 — Discovery & Analysis:
→ Read: traefik://migration/nginx-ingress-scan
         — Inventory all NGINX Ingress resources (paths, annotations, namespaces)
→ Read: traefik://migration/nginx-ingress-analyze
         — Full compatibility report: supported / unsupported / BREAKING annotations
→ Present to user:
     ✅ Supported annotations: [list — will be auto-converted]
     ⚠️  Unsupported annotations: [list — will be dropped]
     ❌ Breaking annotations: [list — REQUIRE agentic overrides or manual work]

Phase 2 — Generate & Review:
→ Traefik:traefik_nginx_migration (action='generate', namespace='...')
   — Produces full Traefik CRD bundle as YAML: IngressRoutes, Middlewares, TraefikServices
   — For breaking annotations: prepare migration_plan overrides
     Example: ignore 'auth-url' annotation + inject custom middleware 'agent-custom-auth'
→ Show YAML bundle to user
→ Confirm before applying

Phase 3 — Apply:
→ Traefik:traefik_nginx_migration (action='apply', namespace='...', migration_plan=overrides)
   — Creates all Traefik CRDs
   — Patches original Ingresses (adds Traefik annotations, removes NGINX ones)
   — Requires MCP_ALLOW_WRITE=true

Phase 4 — Validate:
→ Read: traefik://traffic/routes/list
         — Confirm new IngressRoutes are live
→ Read: traefik://metrics/{ns}/{svc}/summary
         — Confirm traffic flowing without error spikes
→ Read: traefik://migration/nginx-to-traefik
         — Migration status overview

Rollback (single Ingress):
→ Traefik:traefik_nginx_migration (action='revert', ingress_name='...', namespace='...')
   — Undoes the migration for that specific Ingress
   — Restores original NGINX Ingress config
```

**Supervised Autonomy for breaking annotations:**
When NGINX uses annotations with no direct Traefik equivalent (e.g., `nginx.ingress.kubernetes.io/auth-url`), pass a `migration_plan` dict with per-Ingress instructions:
- `ignore_annotations`: list of annotation keys to skip
- `inject_middlewares`: list of custom Traefik middleware names to attach instead

---

## 7. TCP Routing

**Trigger phrases:** "TCP route", "route PostgreSQL", "route Redis", "route MQTT", "TCP through Traefik", "TCP IP allowlist"

```
Step 1 [Optional — IP restriction]:
→ Traefik:traefik_configure_tcp_middleware (action='create')
  params:
    middleware_name: "db-allowlist"
    namespace: "default"
    source_range: ["192.168.1.0/24", "10.0.0.1"]

Step 2 → Traefik:traefik_manage_tcp_routing (action='create')
  params:
    route_name:      name for the IngressRouteTCP
    namespace:       target namespace
    service_name:    K8s Service name (e.g., postgres)
    service_port:    e.g., 5432
    sni:             SNI hostname (e.g., postgres.example.com) — optional for TLS passthrough
    middlewares:     ["db-allowlist"]   — optional, attach TCP middleware
    tls_passthrough: true              — for TLS passthrough (no termination)

Step 3 → Confirm route created
         — Note: TCP routes have no health-based rollback; verify service readiness first

Step 4 → Test TCP connectivity from a client pod
```

**Protocol-specific tips:**
- PostgreSQL: use SNI if TLS enabled; use `tls_passthrough=true` to avoid cert complexity
- Redis: typically non-TLS; IP allowlist is the primary security control
- MQTT: port 1883 (non-TLS) or 8883 (TLS); consider IP allowlist middleware

---

## 8. TLS Termination Setup

**Trigger phrases:** "enable TLS", "HTTPS route", "TLS termination", "attach TLS secret"

```
Step 1 → Confirm TLS Secret exists in the target namespace
         (kubectl get secret {tls_secret_name} -n {namespace})
         Secret must contain: tls.crt + tls.key

Step 2a [New route with TLS]:
→ Traefik:traefik_manage_weighted_routing (action='create')
  params:
    tls_enabled: true
    tls_secret_name: "api-tls"
  — Uses 'websecure' entrypoint automatically

Step 2b [Existing route — recreate with TLS]:
→ Traefik:traefik_generate_routing_manifest (manifest_type='ingress_for_traefik_service')
  — Generate updated IngressRoute YAML with TLS block
  → Review YAML → delete existing → create new with TLS enabled

Step 3 → Read: traefik://traffic/{ns}/{route}/distribution
         — Confirm TLS secret binding and websecure entrypoint active
```

---

## 9. Sticky Sessions

**Trigger phrases:** "sticky sessions", "session affinity", "persist user to same pod", "sticky cookie"

```
Step 1 → Traefik:traefik_configure_service_affinity (action='enable')
  params:
    service_name: "api-svc"
    namespace:    "production"
    cookie_name:  "SESSIONID"   (custom cookie name; default: _traefik_backend)
    max_age:      3600           (seconds; 0 = session cookie)

Step 2 → Confirm sticky annotation applied to K8s Service
         — Traefik reads the annotation and pins subsequent requests

Step 3 → Test: verify repeated requests from same client hit same pod

Disable:
→ Traefik:traefik_configure_service_affinity (action='disable')
  — Removes sticky annotation; subsequent requests load-balance normally
```

**Sticky sessions + canary:** Sticky sessions apply at the Service level. When using weighted routing, users assigned to the canary service remain pinned to canary pods. This is intentional for stateful canary testing.

---

## 10. Traffic Investigation

**Trigger phrases:** "traffic anomaly", "error spike", "latency issue", "route not working", "debugging traffic"

```
Step 1 → Read: traefik://anomalies/detected
         — Real-time anomalies: connection errors, unusual error patterns, affected routes

Step 2 → Read: traefik://metrics/{ns}/{svc}/summary
         — Error rate (4xx/5xx) + P99 latency vs. baseline
         — Identify: is the issue on stable, canary, or both?

Step 3 → Read: traefik://traffic/{ns}/{route}/distribution
         — Confirm: current weights, attached middlewares, match rules
         — Check: has a middleware (e.g., rate_limit, circuit_breaker) triggered?

Step 4 → Read: traefik://anomalies/history/{ns}
         — Historical timeline: when did the anomaly start?
         — Correlate with recent weight shifts or middleware changes

Step 5 → Root cause categories:
     a) Error spike after weight shift → revert weights (update to previous split)
     b) Circuit breaker triggered      → check backend health; detach CB temporarily if false positive
     c) Rate limit too aggressive      → update middleware (lower average or increase burst)
     d) Route unreachable              → verify IngressRoute match rules and entrypoint
     e) TLS error                      → verify Secret exists and is not expired

Step 6 → Report:
     🔍 Anomaly: [type + affected route]
     📊 Metrics: [error rate % + P99 latency]
     🎯 Root Cause: [identified cause]
     💡 Immediate Action: [specific tool call or config change]
     🛡️ Preventive Measure: [circuit breaker, rate limit, monitoring alert]
```
