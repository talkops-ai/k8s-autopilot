# Traefik IngressRoute - Comprehensive Guide

## Table of Contents

1. [Introduction to Traefik](#introduction-to-traefik)
2. [Why Traefik Instead of Standard Ingress?](#why-traefik-instead-of-standard-ingress)
3. [Traefik Architecture and Concepts](#traefik-architecture-and-concepts)
4. [Traefik IngressRoute Tool](#traefik-ingressroute-tool)
5. [Traefik CRDs and Resources](#traefik-crds-and-resources)
6. [Matcher Syntax](#matcher-syntax)
7. [Middleware System](#middleware-system)
8. [Advanced Load Balancing](#advanced-load-balancing)
9. [TLS Configuration](#tls-configuration)
10. [Integration with Coordinator](#integration-with-coordinator)
11. [Examples and Use Cases](#examples-and-use-cases)
12. [Best Practices](#best-practices)
13. [Migration Guide](#migration-guide)
14. [Troubleshooting](#troubleshooting)

---

## Introduction to Traefik

### What is Traefik?

**Traefik** is a modern, cloud-native reverse proxy and load balancer designed for microservices and containerized applications. Unlike traditional ingress controllers that rely on standard Kubernetes Ingress resources, Traefik uses **Custom Resource Definitions (CRDs)** for more powerful and flexible routing configuration.

### Key Characteristics

- **Dynamic Configuration**: Automatically discovers services and updates routing rules
- **Let's Encrypt Integration**: Built-in ACME support for automatic TLS certificate management
- **Middleware System**: Powerful plugin system for request/response manipulation
- **Advanced Load Balancing**: Weighted Round Robin, Consistent Hashing, Traffic Mirroring
- **Multi-Protocol Support**: HTTP, TCP, UDP, WebSocket
- **Observability**: Built-in metrics, tracing, and logging

### Traefik vs. Standard Kubernetes Ingress

| Feature | Standard Ingress | Traefik IngressRoute |
|---------|-----------------|---------------------|
| **API Version** | `networking.k8s.io/v1` | `traefik.io/v1alpha1` |
| **Resource Type** | Ingress (built-in) | IngressRoute (CRD) |
| **Matcher Syntax** | Limited (host, path) | Rich (host, path, headers, methods, query) |
| **Middleware** | Annotations only | Dedicated Middleware CRDs |
| **Load Balancing** | Basic round-robin | WRR, HRW, Mirroring |
| **TLS** | Secret-based or cert-manager | Built-in ACME + cert-manager |
| **Protocols** | HTTP/HTTPS only | HTTP, TCP, UDP, WebSocket |
| **Configuration** | Static YAML | Dynamic discovery |

---

## Why Traefik Instead of Standard Ingress?

### Limitations of Standard Kubernetes Ingress

1. **Limited Routing Capabilities**
   - Only supports host and path-based routing
   - No header-based, method-based, or query parameter routing
   - Limited path matching (Prefix, Exact, ImplementationSpecific)

2. **Annotation Hell**
   - Controller-specific features require annotations
   - Annotations are strings, not structured data
   - Hard to validate and maintain
   - Different syntax for different controllers

3. **No Built-in Middleware**
   - Middleware functionality requires annotations
   - Limited middleware options
   - Hard to compose multiple middlewares

4. **Basic Load Balancing**
   - Only round-robin load balancing
   - No weighted routing
   - No traffic mirroring
   - No sticky sessions (without annotations)

5. **TLS Management Complexity**
   - Requires cert-manager for automatic certificates
   - No built-in ACME support
   - Manual secret management

### Advantages of Traefik IngressRoute

1. **Rich Matcher Syntax**
   ```yaml
   # Standard Ingress: Only host and path
   # Traefik: Host, Path, Headers, Methods, Query, and more
   match: Host(`api.example.com`) && PathPrefix(`/v1`) && Header(`X-API-Version`, `v1`)
   ```

2. **Dedicated Middleware Resources**
   ```yaml
   # Standard Ingress: Annotations
   # annotations:
   #   nginx.ingress.kubernetes.io/rate-limit: "100"
   
   # Traefik: Dedicated CRD
   apiVersion: traefik.io/v1alpha1
   kind: Middleware
   spec:
     rateLimit:
       average: 100
   ```

3. **Advanced Load Balancing**
   ```yaml
   # Weighted Round Robin
   weighted:
     services:
     - name: svc1
       weight: 70
     - name: svc2
       weight: 30
   ```

4. **Built-in ACME Support**
   ```yaml
   tls:
     certResolver: letsencrypt  # Automatic certificate management
   ```

5. **Multi-Protocol Support**
   - HTTP/HTTPS (IngressRoute)
   - TCP (IngressRouteTCP)
   - UDP (IngressRouteUDP)

---

## Traefik Architecture and Concepts

### Core Components

```
┌─────────────────────────────────────────────────────────┐
│                    Traefik Proxy                         │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Entry Points │  │   Routers    │  │  Middlewares │ │
│  │              │  │              │  │              │ │
│  │ - web (80)   │  │ - Matchers   │  │ - RateLimit  │ │
│  │ - websecure  │  │ - Services   │  │ - BasicAuth  │ │
│  │   (443)      │  │ - TLS        │  │ - CORS       │ │
│  │ - tcp (8080) │  │ - Priority   │  │ - StripPrefix│ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│         │                  │                  │        │
│         └──────────────────┼──────────────────┘        │
│                            │                            │
│                    ┌───────▼────────┐                    │
│                    │   Services     │                    │
│                    │  (Load Balancer)│                    │
│                    └───────┬────────┘                    │
│                            │                            │
└────────────────────────────┼────────────────────────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Kubernetes     │
                    │  Services       │
                    └─────────────────┘
```

### Entry Points

**Entry Points** are the network ports that Traefik listens on.

**Common Entry Points**:
- `web` - HTTP (port 80)
- `websecure` - HTTPS (port 443)
- `tcp` - TCP (custom port)
- `udp` - UDP (custom port)

**Configuration**:
```yaml
spec:
  entryPoints:
  - web          # HTTP traffic
  - websecure    # HTTPS traffic
```

### Routers

**Routers** match incoming requests and route them to services.

**Components**:
- **Matchers**: Conditions that must be met (Host, Path, Headers, etc.)
- **Services**: Backend services to route to
- **Middlewares**: Request/response transformations
- **Priority**: Disambiguation when multiple routes match
- **TLS**: TLS termination configuration

### Services

**Services** define backend targets and load balancing strategies.

**Types**:
1. **Simple Service**: Direct Kubernetes Service reference
2. **TraefikService**: Advanced load balancing (WRR, HRW, Mirroring)

### Middlewares

**Middlewares** transform requests and responses.

**Types**:
- **RateLimit**: Rate limiting
- **BasicAuth**: HTTP Basic Authentication
- **CORS**: Cross-Origin Resource Sharing
- **StripPrefix**: Remove path prefix
- **RedirectScheme**: HTTP to HTTPS redirect
- **ForwardAuth**: External authentication
- **Headers**: Add/remove/modify headers
- **IPWhitelist**: IP-based access control
- **Compress**: Response compression
- **Retry**: Retry failed requests

---

## Traefik IngressRoute Tool

### Tool Overview

**Tool Name**: `generate_traefik_ingressroute_yaml`

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/route/traefik_ingressroute_tool.py`

**Purpose**: Generate Traefik IngressRoute CRD manifests with Helm templating

**Execution**: Conditional tool (executed when Ingress resource exists in planner output)

**Dependencies**: 
- `["generate_service_yaml"]` - Service must exist
- `["generate_helpers_tpl"]` - Helper templates for labels/names

### Input Extraction

The tool extracts configuration from planner output:

```python
# From planner_output.kubernetes_architecture.resources.auxiliary
ingress_resource = find_resource_by_type("Ingress")

# Extract configuration hints
config_hints = ingress_resource.get("configuration_hints", {})

# Extract routing rules
rules = config_hints.get("rules", [])
hosts = config_hints.get("hosts", [])
tls_config = config_hints.get("tls", {})

# Extract Traefik-specific config
traefik_config = config_hints.get("traefik", {})
annotations = traefik_config.get("annotations", {})
```

### Output Schema

```python
class TraefikIngressRouteToolOutput(BaseModel):
    yaml_content: str  # Complete IngressRoute YAML
    file_name: str  # "ingressroute.yaml", "ingressroutetcp.yaml", or "ingressrouteudp.yaml"
    middleware_yaml: Optional[str]  # Generated Middleware resources (if any)
    traefik_service_yaml: Optional[str]  # Generated TraefikService (if advanced LB)
    template_variables_used: List[str]  # All {{ .Values.* }} references
    helm_template_functions_used: List[str]  # Helper function calls
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str]
    metadata: Dict[str, Any]
    
    # Traefik-specific metadata
    route_type: str  # "HTTP", "TCP", or "UDP"
    matcher_rules: List[str]  # Traefik matcher rules
    entry_points: List[str]  # Entry points used
    middlewares_referenced: List[str]  # Middleware names referenced
    services_referenced: List[str]  # Service names referenced
    tls_enabled: bool
    cert_resolver_used: Optional[str]
```

### Tool Execution Flow

```
1. Extract Ingress resource from planner output
   ↓
2. Parse routing rules (hosts, paths, services)
   ↓
3. Build Traefik matcher rules from Kubernetes Ingress rules
   ↓
4. Extract TLS configuration
   ↓
5. Extract Traefik-specific configuration (entry points, middlewares)
   ↓
6. Format user prompt with all configuration
   ↓
7. Call LLM with Traefik-specific system prompt
   ↓
8. Parse and validate output
   ↓
9. Update state with generated IngressRoute YAML
```

### Matcher Rule Building

The tool converts Kubernetes Ingress rules to Traefik matcher syntax:

```python
# Kubernetes Ingress Rule
{
    "host": "api.example.com",
    "http": {
        "paths": [{
            "path": "/api",
            "pathType": "Prefix",
            "service": {"name": "myapp", "port": {"number": 80}}
        }]
    }
}

# Converts to Traefik Matcher
"Host(`api.example.com`) && PathPrefix(`/api`)"
```

---

## Traefik CRDs and Resources

### 1. IngressRoute (HTTP/HTTPS)

**API Version**: `traefik.io/v1alpha1`  
**Kind**: `IngressRoute`

**Purpose**: Route HTTP/HTTPS traffic to Kubernetes services

**Structure**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
  namespace: {{ .Values.namespace.name | default .Release.Namespace }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  entryPoints:
  - web
  - websecure
  routes:
  - match: Host(`api.example.com`) && PathPrefix(`/api`)
    kind: Rule
    priority: 10
    middlewares:
    - name: ratelimit
      namespace: default
    services:
    - name: {{ include "myapp.fullname" . }}
      port: {{ .Values.service.port }}
      weight: 1
  tls:
    certResolver: letsencrypt
    domains:
    - main: api.example.com
```

### 2. IngressRouteTCP (TCP)

**API Version**: `traefik.io/v1alpha1`  
**Kind**: `IngressRouteTCP`

**Purpose**: Route TCP traffic (e.g., databases, SSH)

**Structure**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRouteTCP
metadata:
  name: {{ include "myapp.fullname" . }}-tcp
spec:
  entryPoints:
  - tcp
  routes:
  - match: HostSNI(`db.example.com`)
    services:
    - name: {{ include "myapp.fullname" . }}-db
      port: {{ .Values.db.port }}
      weight: 1
  tls:
    secretName: {{ .Values.tls.secretName }}
```

**Key Differences**:
- Uses `HostSNI()` matcher instead of `Host()`
- No path-based routing (TCP doesn't have paths)
- TLS passthrough supported

### 3. IngressRouteUDP (UDP)

**API Version**: `traefik.io/v1alpha1`  
**Kind**: `IngressRouteUDP`

**Purpose**: Route UDP traffic (e.g., DNS, syslog)

**Structure**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRouteUDP
metadata:
  name: {{ include "myapp.fullname" . }}-udp
spec:
  entryPoints:
  - udp
  routes:
  - services:
    - name: {{ include "myapp.fullname" . }}-udp-service
      port: {{ .Values.udp.port }}
      weight: 1
```

**Key Differences**:
- No matchers (UDP doesn't have host/path)
- Simple service routing only

### 4. Middleware

**API Version**: `traefik.io/v1alpha1`  
**Kind**: `Middleware`

**Purpose**: Transform requests/responses

**Common Middleware Types**:

#### RateLimit Middleware
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: ratelimit
spec:
  rateLimit:
    average: 100
    burst: 200
    sourceCriterion:
      ipStrategy:
        depth: 1
```

#### BasicAuth Middleware
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: basicauth
spec:
  basicAuth:
    users:
    - "user:$apr1$H6uskkkW$IgX3Se3q63MdIwuPR3Ftp."
    removeHeader: true
```

#### CORS Middleware
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: cors
spec:
  cors:
    allowedOrigins:
    - "https://example.com"
    - "https://www.example.com"
    allowedMethods:
    - GET
    - POST
    - PUT
    allowedHeaders:
    - "*"
    maxAge: 3600
    allowCredentials: true
```

#### StripPrefix Middleware
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: stripprefix
spec:
  stripPrefix:
    prefixes:
    - "/api/v1"
    - "/api/v2"
```

#### ForwardAuth Middleware
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: forwardauth
spec:
  forwardAuth:
    address: "http://auth-service:3000/auth"
    trustForwardHeader: true
    authResponseHeaders:
    - X-Remote-User
    - X-Remote-Groups
    authRequestHeaders:
    - X-Forwarded-User
```

#### Headers Middleware
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: security-headers
spec:
  headers:
    customRequestHeaders:
      X-Forwarded-Proto: "https"
    customResponseHeaders:
      X-Content-Type-Options: "nosniff"
      X-Frame-Options: "DENY"
      X-XSS-Protection: "1; mode=block"
      Strict-Transport-Security: "max-age=31536000; includeSubDomains"
```

### 5. TraefikService (Advanced Load Balancing)

**API Version**: `traefik.io/v1alpha1`  
**Kind**: `TraefikService`

**Purpose**: Advanced load balancing strategies

**Types**:

#### Weighted Round Robin (WRR)
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: wrr-service
spec:
  weighted:
    services:
    - name: svc-v1
      port: 80
      weight: 70
    - name: svc-v2
      port: 80
      weight: 30
    sticky:
      cookie:
        name: sessionid
        maxAge: 3600
```

#### Highest Random Weight (HRW)
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: hrw-service
spec:
  weighted:
    services:
    - name: svc-1
      port: 80
      weight: 1
    - name: svc-2
      port: 80
      weight: 1
    strategy: HighestRandomWeight
```

#### Mirroring
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: mirror-service
spec:
  mirroring:
    service: main-service
    mirrors:
    - name: shadow-service
      percent: 10  # Mirror 10% of traffic
```

---

## Matcher Syntax

### Basic Matchers

#### Host Matcher
```yaml
match: Host(`example.com`)
match: Host(`api.example.com`) || Host(`www.example.com`)
```

#### Path Matchers
```yaml
match: Path(`/exact/path`)
match: PathPrefix(`/api`)
match: PathRegexp(`^/api/v[0-9]+$`)
```

#### Method Matcher
```yaml
match: Method(`GET`, `POST`)
match: Method(`GET`) || Method(`POST`)
```

#### Header Matchers
```yaml
match: Header(`X-Custom`, `value`)
match: HeaderRegexp(`X-API-Version`, `v[0-9]+`)
match: Header(`Content-Type`, `application/json`)
```

#### Query Matchers
```yaml
match: Query(`version`, `v1`)
match: QueryRegexp(`version`, `v[0-9]+`)
```

### Combining Matchers

**AND Operator (`&&`)**:
```yaml
match: Host(`api.example.com`) && PathPrefix(`/v1`) && Header(`X-API-Version`, `v1`)
```

**OR Operator (`||`)**:
```yaml
match: Path(`/v1`) || Path(`/v2`)
match: Host(`api.example.com`) || Host(`api-staging.example.com`)
```

**Complex Combinations**:
```yaml
match: (Host(`api.example.com`) || Host(`api-staging.example.com`)) && PathPrefix(`/api`) && Method(`GET`, `POST`)
```

### Matcher Priority

When multiple routes match, Traefik uses **priority** to disambiguate:

```yaml
routes:
- match: Host(`api.example.com`) && PathPrefix(`/api/v1`)
  priority: 20  # Higher priority (checked first)
  services:
  - name: v1-service
- match: Host(`api.example.com`) && PathPrefix(`/api`)
  priority: 10  # Lower priority (checked second)
  services:
  - name: default-service
```

**Priority Rules**:
- Lower number = Higher priority
- More specific routes should have higher priority
- Default priority is 0

---

## Middleware System

### Middleware Order

Middlewares are applied in the **order they appear** in the `middlewares` list:

```yaml
routes:
- match: Host(`api.example.com`)
  middlewares:
  - name: ratelimit      # Applied first
  - name: basicauth      # Applied second
  - name: cors           # Applied third
  - name: stripprefix    # Applied fourth
  services:
  - name: api-service
```

**Execution Order**:
1. RateLimit (reject if over limit)
2. BasicAuth (authenticate user)
3. CORS (add CORS headers)
4. StripPrefix (remove `/api` prefix)
5. Forward to service

### Middleware Composition

**Best Practice**: Compose multiple middlewares for complex scenarios:

```yaml
# Security Middleware Chain
middlewares:
- name: ipwhitelist      # Allow only specific IPs
- name: basicauth        # Require authentication
- name: security-headers # Add security headers
- name: compress         # Compress response
```

### Creating Middlewares

Middlewares are **separate resources** that can be referenced:

```yaml
# Middleware definition
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: ratelimit
  namespace: default
spec:
  rateLimit:
    average: 100
    burst: 200

# IngressRoute references middleware
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
spec:
  routes:
  - match: Host(`api.example.com`)
    middlewares:
    - name: ratelimit
      namespace: default
    services:
    - name: api-service
```

---

## Advanced Load Balancing

### Weighted Round Robin (WRR)

**Use Case**: Gradual rollout, A/B testing, canary deployments

**Example**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: canary-service
spec:
  weighted:
    services:
    - name: stable-service
      port: 80
      weight: 90  # 90% of traffic
    - name: canary-service
      port: 80
      weight: 10  # 10% of traffic
```

**Sticky Sessions**:
```yaml
weighted:
  services:
  - name: svc-1
    weight: 50
  - name: svc-2
    weight: 50
  sticky:
    cookie:
      name: sessionid
      maxAge: 3600
      secure: true
      httpOnly: true
      sameSite: "Lax"
```

### Highest Random Weight (HRW)

**Use Case**: Consistent hashing by client IP

**Example**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: hrw-service
spec:
  weighted:
    services:
    - name: svc-1
      port: 80
      weight: 1
    - name: svc-2
      port: 80
      weight: 1
    strategy: HighestRandomWeight
```

### Traffic Mirroring

**Use Case**: Testing new versions, shadow traffic, debugging

**Example**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: mirror-service
spec:
  mirroring:
    service: main-service
    mirrors:
    - name: shadow-service-v2
      percent: 10  # Mirror 10% of traffic
    - name: shadow-service-v3
      percent: 5   # Mirror 5% of traffic
```

**Note**: Mirrored traffic doesn't affect the main service response. Responses from mirrors are ignored.

---

## TLS Configuration

### TLS Termination

**Option 1: Kubernetes Secret**
```yaml
tls:
  secretName: {{ .Values.tls.secretName }}
```

**Option 2: ACME (Let's Encrypt)**
```yaml
tls:
  certResolver: letsencrypt
  domains:
  - main: api.example.com
    sans:
    - www.api.example.com
```

**Option 3: TLS Passthrough**
```yaml
tls:
  passthrough: true  # Forward TLS to backend
```

### TLS Options

**TLS Options** configure TLS behavior:

```yaml
# TLS Options resource
apiVersion: traefik.io/v1alpha1
kind: TLSOption
metadata:
  name: modern-tls
spec:
  minVersion: VersionTLS12
  cipherSuites:
  - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
  - TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305
  - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
  sniStrict: true

# Reference in IngressRoute
tls:
  certResolver: letsencrypt
  options:
    name: modern-tls
    namespace: default
```

### Certificate Resolvers

**ACME Configuration** (in Traefik static config):

```yaml
certificatesResolvers:
  letsencrypt:
    acme:
      email: admin@example.com
      storage: /data/acme.json
      httpChallenge:
        entryPoint: web
      # OR
      dnsChallenge:
        provider: cloudflare
```

**Usage in IngressRoute**:
```yaml
tls:
  certResolver: letsencrypt
  domains:
  - main: api.example.com
```

---

## Integration with Coordinator

### Tool Detection

The coordinator detects Traefik IngressRoute tool when:

```python
# In auxiliary resources
{
    "type": "Ingress",
    "configuration_hints": {
        "traefik": {
            "enabled": true
        }
    }
}
```

### Execution Order

```
1. generate_service_yaml ✓ (Service must exist)
2. generate_helpers_tpl ✓ (For labels/names)
   ↓
3. generate_traefik_ingressroute_yaml
   ├─ Extracts Ingress resource from planner
   ├─ Converts Kubernetes rules to Traefik matchers
   ├─ Generates IngressRoute YAML
   └─ Optionally generates Middleware/TraefikService YAML
   ↓
4. Updates state:
   ├─ generated_templates["ingressroute.yaml"] = yaml_content
   ├─ template_variables_used += [...]
   └─ completed_tools += ["generate_traefik_ingressroute_yaml"]
```

### Dependencies

**Required**:
- `generate_service_yaml` - Service must exist for routing
- `generate_helpers_tpl` - Helper templates for consistent naming

**Optional**:
- Middlewares can be defined separately (not generated by this tool)
- TraefikService can be generated if advanced LB needed

### State Updates

```python
Command(update={
    "generated_templates": {
        "ingressroute.yaml": traefik_yaml_content,
        # Optionally:
        # "middleware.yaml": middleware_yaml_content,
        # "traefikservice.yaml": traefik_service_yaml_content
    },
    "template_variables_used": [
        ".Values.service.port",
        ".Values.ingress.hostname",
        ".Values.ingress.tls.certResolver",
        ...
    ],
    "completed_tools": ["generate_traefik_ingressroute_yaml"],
    "tool_results": {
        "generate_traefik_ingressroute_yaml": {
            "status": "success",
            "output": {
                "route_type": "HTTP",
                "matcher_rules": ["Host(`api.example.com`)"],
                "entry_points": ["websecure"],
                "tls_enabled": true,
                "cert_resolver_used": "letsencrypt"
            }
        }
    }
})
```

---

## Examples and Use Cases

### Example 1: Simple HTTP API

**Requirements**:
- Expose API at `api.example.com`
- HTTPS with Let's Encrypt
- Rate limiting

**Generated IngressRoute**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
  namespace: {{ .Values.namespace.name | default .Release.Namespace }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  entryPoints:
  - websecure
  routes:
  - match: Host(`api.example.com`)
    kind: Rule
    priority: 10
    middlewares:
    - name: ratelimit
      namespace: {{ .Release.Namespace }}
    services:
    - name: {{ include "myapp.fullname" . }}
      port: {{ .Values.service.port }}
      weight: 1
  tls:
    certResolver: {{ .Values.ingress.tls.certResolver }}
    domains:
    - main: api.example.com
```

**Values.yaml**:
```yaml
service:
  port: 80

ingress:
  hostname: api.example.com
  tls:
    certResolver: letsencrypt
```

### Example 2: Path-Based Routing

**Requirements**:
- Multiple paths: `/api/v1`, `/api/v2`, `/admin`
- Different services for each path
- Authentication for `/admin`

**Generated IngressRoute**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
spec:
  entryPoints:
  - websecure
  routes:
  # API v1
  - match: Host(`api.example.com`) && PathPrefix(`/api/v1`)
    kind: Rule
    priority: 20
    services:
    - name: {{ include "myapp.fullname" . }}-v1
      port: {{ .Values.service.port }}
  # API v2
  - match: Host(`api.example.com`) && PathPrefix(`/api/v2`)
    kind: Rule
    priority: 20
    services:
    - name: {{ include "myapp.fullname" . }}-v2
      port: {{ .Values.service.port }}
  # Admin (with auth)
  - match: Host(`api.example.com`) && PathPrefix(`/admin`)
    kind: Rule
    priority: 20
    middlewares:
    - name: basicauth
      namespace: {{ .Release.Namespace }}
    services:
    - name: {{ include "myapp.fullname" . }}-admin
      port: {{ .Values.service.port }}
  tls:
    certResolver: {{ .Values.ingress.tls.certResolver }}
```

### Example 3: Canary Deployment

**Requirements**:
- 90% traffic to stable version
- 10% traffic to canary version
- Sticky sessions

**Generated Resources**:

**TraefikService**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: {{ include "myapp.fullname" . }}-canary
spec:
  weighted:
    services:
    - name: {{ include "myapp.fullname" . }}-stable
      port: {{ .Values.service.port }}
      weight: 90
    - name: {{ include "myapp.fullname" . }}-canary
      port: {{ .Values.service.port }}
      weight: 10
    sticky:
      cookie:
        name: sessionid
        maxAge: 3600
```

**IngressRoute**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
spec:
  entryPoints:
  - websecure
  routes:
  - match: Host(`api.example.com`)
    kind: Rule
    services:
    - name: {{ include "myapp.fullname" . }}-canary
      kind: TraefikService
      port: {{ .Values.service.port }}
  tls:
    certResolver: {{ .Values.ingress.tls.certResolver }}
```

### Example 4: Header-Based Routing

**Requirements**:
- Route based on `X-API-Version` header
- v1 → v1 service
- v2 → v2 service

**Generated IngressRoute**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
spec:
  entryPoints:
  - websecure
  routes:
  # API v1 (header-based)
  - match: Host(`api.example.com`) && Header(`X-API-Version`, `v1`)
    kind: Rule
    priority: 20
    services:
    - name: {{ include "myapp.fullname" . }}-v1
      port: {{ .Values.service.port }}
  # API v2 (header-based)
  - match: Host(`api.example.com`) && Header(`X-API-Version`, `v2`)
    kind: Rule
    priority: 20
    services:
    - name: {{ include "myapp.fullname" . }}-v2
      port: {{ .Values.service.port }}
  # Default (no header)
  - match: Host(`api.example.com`)
    kind: Rule
    priority: 10
    services:
    - name: {{ include "myapp.fullname" . }}-v1
      port: {{ .Values.service.port }}
  tls:
    certResolver: {{ .Values.ingress.tls.certResolver }}
```

### Example 5: TCP Routing (Database)

**Requirements**:
- Expose PostgreSQL database via TCP
- TLS termination
- SNI-based routing

**Generated IngressRouteTCP**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRouteTCP
metadata:
  name: {{ include "myapp.fullname" . }}-db
spec:
  entryPoints:
  - tcp
  routes:
  - match: HostSNI(`db.example.com`)
    services:
    - name: {{ include "myapp.fullname" . }}-postgresql
      port: {{ .Values.db.port }}
      weight: 1
  tls:
    secretName: {{ .Values.tls.secretName }}
```

---

## Best Practices

### 1. Matcher Syntax

**DO**:
```yaml
match: Host(`api.example.com`) && PathPrefix(`/api`)
```

**DON'T**:
```yaml
match: Host(`api.example.com`) && Path(`/api/*`)  # Wrong syntax
```

### 2. Priority Management

**DO**: Use explicit priorities for disambiguation
```yaml
routes:
- match: Host(`api.example.com`) && PathPrefix(`/api/v1`)
  priority: 20  # More specific = higher priority
- match: Host(`api.example.com`) && PathPrefix(`/api`)
  priority: 10  # Less specific = lower priority
```

**DON'T**: Rely on default priority (0) for all routes

### 3. Middleware Order

**DO**: Order middlewares logically
```yaml
middlewares:
- name: ipwhitelist    # Security first
- name: basicauth      # Authentication second
- name: ratelimit      # Rate limiting third
- name: cors           # CORS headers last
```

**DON'T**: Put rate limiting after authentication (wastes auth resources)

### 4. TLS Configuration

**DO**: Use certResolver for automatic certificates
```yaml
tls:
  certResolver: letsencrypt
  domains:
  - main: api.example.com
```

**DON'T**: Hardcode secret names (use Helm templating)
```yaml
tls:
  secretName: "hardcoded-secret"  # Bad
```

**DO**:
```yaml
tls:
  secretName: {{ .Values.tls.secretName }}  # Good
```

### 5. Service References

**DO**: Use helper templates for consistency
```yaml
services:
- name: {{ include "myapp.fullname" . }}
  port: {{ .Values.service.port }}
```

**DON'T**: Hardcode service names
```yaml
services:
- name: "myapp-service"  # Bad - breaks with different release names
```

### 6. Entry Points

**DO**: Use appropriate entry points
```yaml
entryPoints:
- websecure  # HTTPS only
```

**DON'T**: Mix HTTP and HTTPS unnecessarily
```yaml
entryPoints:
- web        # HTTP
- websecure  # HTTPS
# Only if you need both
```

### 7. Error Handling

**DO**: Define error pages
```yaml
# In Traefik static config
entryPoints:
  websecure:
    address: ":443"
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
          permanent: true
```

### 8. Health Checks

**DO**: Let Traefik handle health checks automatically
- Traefik monitors backend service health
- Unhealthy services are automatically removed from rotation

**DON'T**: Implement health checks in middleware (Traefik handles this)

---

## Migration Guide

### From Standard Kubernetes Ingress to Traefik IngressRoute

#### Step 1: Identify Current Ingress Configuration

**Standard Ingress**:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp-ingress
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - api.example.com
    secretName: api-tls
  rules:
  - host: api.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: myapp-service
            port:
              number: 80
```

#### Step 2: Convert to Traefik IngressRoute

**Traefik IngressRoute**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  entryPoints:
  - websecure
  routes:
  - match: Host(`api.example.com`)
    kind: Rule
    services:
    - name: {{ include "myapp.fullname" . }}
      port: {{ .Values.service.port }}
  tls:
    certResolver: letsencrypt
    domains:
    - main: api.example.com
```

#### Step 3: Convert Annotations to Middlewares

**Before (Annotations)**:
```yaml
annotations:
  nginx.ingress.kubernetes.io/rate-limit: "100"
  nginx.ingress.kubernetes.io/auth-type: basic
  nginx.ingress.kubernetes.io/cors-allow-origin: "*"
```

**After (Middlewares)**:
```yaml
# Middleware resources
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: ratelimit
spec:
  rateLimit:
    average: 100
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: basicauth
spec:
  basicAuth:
    users:
    - "user:$apr1$..."
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: cors
spec:
  cors:
    allowedOrigins:
    - "*"

# Reference in IngressRoute
spec:
  routes:
  - match: Host(`api.example.com`)
    middlewares:
    - name: ratelimit
    - name: basicauth
    - name: cors
    services:
    - name: myapp-service
```

### Migration Checklist

- [ ] Install Traefik with CRDs
- [ ] Convert Ingress rules to Traefik matchers
- [ ] Convert annotations to Middleware resources
- [ ] Update TLS configuration (certResolver vs secretName)
- [ ] Test routing rules
- [ ] Verify TLS certificates
- [ ] Update monitoring/alerting (if needed)
- [ ] Remove old Ingress resources
- [ ] Update documentation

---

## Troubleshooting

### Common Issues

#### 1. Routes Not Matching

**Symptoms**: Requests not reaching backend service

**Debugging**:
```bash
# Check IngressRoute status
kubectl describe ingressroute myapp -n myapp-ns

# Check Traefik logs
kubectl logs -n traefik-system deployment/traefik

# Verify matcher syntax
kubectl get ingressroute myapp -o yaml
```

**Common Causes**:
- Incorrect matcher syntax
- Priority conflicts
- Entry point mismatch
- Service name mismatch

**Solutions**:
- Verify matcher syntax: `Host(\`example.com\`)` (backticks!)
- Check service name matches exactly
- Verify entry points are configured in Traefik

#### 2. TLS Certificate Issues

**Symptoms**: HTTPS not working, certificate errors

**Debugging**:
```bash
# Check certificate status
kubectl get certificates -n myapp-ns

# Check Traefik ACME logs
kubectl logs -n traefik-system deployment/traefik | grep acme

# Verify certResolver configuration
kubectl get ingressroute myapp -o yaml | grep certResolver
```

**Common Causes**:
- certResolver not configured in Traefik static config
- DNS not pointing to Traefik
- HTTP challenge failing (firewall/security group)
- Certificate storage issues

**Solutions**:
- Verify certResolver exists in Traefik config
- Check DNS records
- Ensure port 80/443 accessible
- Check certificate storage permissions

#### 3. Middleware Not Applied

**Symptoms**: Middleware not affecting requests

**Debugging**:
```bash
# Check middleware exists
kubectl get middleware -n myapp-ns

# Verify middleware reference
kubectl get ingressroute myapp -o yaml | grep -A 5 middlewares

# Check Traefik logs
kubectl logs -n traefik-system deployment/traefik | grep middleware
```

**Common Causes**:
- Middleware not created
- Namespace mismatch
- Middleware name typo
- Middleware order issue

**Solutions**:
- Verify middleware resource exists
- Check namespace matches
- Verify middleware name spelling
- Check middleware order

#### 4. Service Not Found

**Symptoms**: 502 Bad Gateway, service unreachable

**Debugging**:
```bash
# Check service exists
kubectl get svc -n myapp-ns

# Verify service name in IngressRoute
kubectl get ingressroute myapp -o yaml | grep -A 3 services

# Check service endpoints
kubectl get endpoints -n myapp-ns
```

**Common Causes**:
- Service doesn't exist
- Service name mismatch
- Service port mismatch
- No healthy endpoints

**Solutions**:
- Verify service exists and is running
- Check service name matches exactly
- Verify port number matches
- Check pod labels match service selector

#### 5. Priority Conflicts

**Symptoms**: Wrong route handling requests

**Debugging**:
```bash
# List all IngressRoutes
kubectl get ingressroute -n myapp-ns

# Check priorities
kubectl get ingressroute -n myapp-ns -o yaml | grep priority
```

**Common Causes**:
- Multiple routes matching same request
- Priority not set or incorrect
- More specific route has lower priority

**Solutions**:
- Set explicit priorities
- More specific routes should have higher priority
- Use unique matchers when possible

### Debugging Commands

```bash
# Check Traefik configuration
kubectl get configmap traefik -n traefik-system -o yaml

# View Traefik dashboard (if enabled)
kubectl port-forward -n traefik-system svc/traefik 8080:8080
# Open http://localhost:8080/dashboard/

# Check IngressRoute events
kubectl describe ingressroute myapp -n myapp-ns

# Test routing
curl -H "Host: api.example.com" http://traefik-ip/api/v1

# Check TLS certificates
kubectl get certificates -n myapp-ns
kubectl describe certificate api-tls -n myapp-ns
```

---

## Advanced Topics

### TraefikService for Complex Routing

**Use Case**: Multiple services with different weights

```yaml
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: {{ include "myapp.fullname" . }}-wrr
spec:
  weighted:
    services:
    - name: {{ include "myapp.fullname" . }}-v1
      port: {{ .Values.service.port }}
      weight: 80
    - name: {{ include "myapp.fullname" . }}-v2
      port: {{ .Values.service.port }}
      weight: 20
    sticky:
      cookie:
        name: sessionid
        maxAge: 3600
```

**Reference in IngressRoute**:
```yaml
services:
- name: {{ include "myapp.fullname" . }}-wrr
  kind: TraefikService
  port: {{ .Values.service.port }}
```

### Middleware Chains

**Best Practice**: Create reusable middleware chains

```yaml
# Security chain
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: security-chain
spec:
  chain:
    middlewares:
    - name: ipwhitelist
    - name: basicauth
    - name: security-headers
```

**Usage**:
```yaml
routes:
- match: Host(`api.example.com`)
  middlewares:
  - name: security-chain  # Single reference instead of 3
  services:
  - name: api-service
```

### Dynamic Configuration

Traefik supports **dynamic configuration** via:
- Kubernetes CRDs (IngressRoute, Middleware, etc.)
- File-based configuration
- Docker labels
- Consul, etcd, Redis

**Kubernetes CRDs** (recommended):
- Declarative
- Version controlled
- Easy to manage
- Integrated with Helm

---

## Integration with Values.yaml

### Recommended Values Structure

```yaml
ingress:
  enabled: true
  hostname: api.example.com
  
  # Traefik-specific
  traefik:
    entryPoints:
    - websecure
    priority: 10
    
    # Middlewares
    middlewares:
    - name: ratelimit
      namespace: default
    - name: cors
      namespace: default
    
    # TLS
    tls:
      enabled: true
      certResolver: letsencrypt
      domains:
      - main: api.example.com
        sans:
        - www.api.example.com
    
    # Advanced options
    passHostHeader: true
    timeout: 30s
```

### Template Variables Used

Common template variables in generated IngressRoute:

```yaml
# Service reference
{{ include "myapp.fullname" . }}
{{ .Values.service.port }}

# Namespace
{{ .Values.namespace.name | default .Release.Namespace }}

# TLS
{{ .Values.ingress.tls.certResolver }}
{{ .Values.ingress.tls.domains }}

# Hostname
{{ .Values.ingress.hostname }}

# Middlewares
{{- range .Values.ingress.middlewares }}
- name: {{ .name }}
  namespace: {{ .namespace | default .Release.Namespace }}
{{- end }}
```

---

## Comparison with Other Ingress Controllers

### Traefik vs. NGINX Ingress

| Feature | NGINX Ingress | Traefik |
|---------|---------------|---------|
| **Configuration** | Annotations | CRDs |
| **Dynamic Updates** | Reload required | Automatic |
| **Middleware** | Limited | Rich |
| **Load Balancing** | Basic | Advanced |
| **TLS** | cert-manager | Built-in ACME |
| **Dashboard** | Third-party | Built-in |
| **Metrics** | Prometheus exporter | Built-in |

### Traefik vs. Istio Gateway

| Feature | Istio Gateway | Traefik |
|---------|---------------|---------|
| **Complexity** | High | Medium |
| **Service Mesh** | Required | Optional |
| **Configuration** | CRDs | CRDs |
| **Learning Curve** | Steep | Moderate |
| **Use Case** | Full mesh | Ingress only |

---

## References

- **Implementation**: `k8s_autopilot/core/agents/helm_generator/template/tools/route/traefik_ingressroute_tool.py`
- **Prompts**: `k8s_autopilot/core/agents/helm_generator/template/tools/route/traefik_ingressroute_prompts.py`
- **Traefik Documentation**: https://doc.traefik.io/traefik/
- **Traefik CRD Reference**: https://doc.traefik.io/traefik/routing/providers/kubernetes-crd/
- **Traefik Matchers**: https://doc.traefik.io/traefik/routing/routers/#rule
- **Traefik Middlewares**: https://doc.traefik.io/traefik/middlewares/overview/

---

**Version**: 1.0  
**Last Updated**: 2025-01-XX  
**Status**: Production-Ready
