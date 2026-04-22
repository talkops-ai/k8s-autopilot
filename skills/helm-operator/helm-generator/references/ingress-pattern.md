# Traefik IngressRoute Pattern

When `ingress.enabled` is true, we utilize Traefik Kubernetes Custom Resource Definitions (CRDs) for advanced routing. Native `Networking/v1 Ingress` is discouraged in favor of `traefik.io/v1alpha1` CRDs: `IngressRoute`, `IngressRouteTCP`, and `Middleware`.

## 1. HTTP/HTTPS IngressRoute

Used for standard web traffic. Supports advanced matchers (Host, PathPrefix, Headers) and Middleware attachments.

```yaml
{{- if and .Values.ingress.enabled (eq (default "http" .Values.ingress.protocol) "http") -}}
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "my-chart.fullname" . }}
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
  {{- with .Values.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  entryPoints:
    {{- toYaml (default (list "web" "websecure") .Values.ingress.entryPoints) | nindent 4 }}
  routes:
    {{- range .Values.ingress.hosts }}
    - match: Host(`{{ .host }}`) {{- if .path }} && PathPrefix(`{{ .path }}`) {{- end }}
      kind: Rule
      services:
        - name: {{ include "my-chart.fullname" $ }}
          port: {{ $.Values.service.port }}
          {{- if $.Values.ingress.sticky }}
          sticky:
            cookie:
              name: {{ include "my-chart.fullname" $ }}-cookie
          {{- end }}
      {{- if or .middlewares $.Values.ingress.middlewares }}
      middlewares:
        {{- range $middleware := (default $.Values.ingress.middlewares .middlewares) }}
        - name: {{ $middleware.name }}
          namespace: {{ default $.Release.Namespace $middleware.namespace }}
        {{- end }}
      {{- end }}
    {{- end }}
  {{- if .Values.ingress.tls }}
  tls:
    {{- if .Values.ingress.tls.secretName }}
    secretName: {{ .Values.ingress.tls.secretName }}
    {{- end }}
    {{- if .Values.ingress.tls.options }}
    options:
      name: {{ .Values.ingress.tls.options.name }}
      namespace: {{ default .Release.Namespace .Values.ingress.tls.options.namespace }}
    {{- end }}
  {{- end }}
{{- end }}
```

## Progressive Disclosure: Advanced Features

If the user requires more than standard HTTP routing, **STOP** and read the corresponding reference file before proceeding:

| Feature Requested | Reference File to Read |
|---|---|
| Modifying URLs (StripPrefix, ReplacePath, Redirects) | `references/traefik-reference/traefik-middlewares-routing.md` |
| Access Control (BasicAuth, IP Whitelisting, RateLimit) | `references/traefik-reference/traefik-middlewares-security.md` |
| Reliability (Retry, CircuitBreaker, Buffering) | `references/traefik-reference/traefik-middlewares-resilience.md` |
| Canary, Mirroring, or Failover | `references/traefik-reference/traefik-advanced-services.md` |
| Database, MQTT, or raw TCP/UDP exposition | `references/traefik-reference/traefik-tcp-udp.md` |

## values.yaml Keys

```yaml
ingress:
  enabled: false
  ## Traefik entrypoints (e.g. web, websecure)
  entryPoints: ["web", "websecure"]
  annotations: {}
  
  ## HTTP Routing
  hosts:
    - host: chart-example.local
      path: /
      # Specific middlewares for this host:
      # middlewares:
      #   - name: basic-auth
  
  ## Global Middlewares to attach to ALL HTTP routes
  middlewares: []
  
  ## Sticky sessions for HTTP services
  sticky: false

  ## TLS Configuration
  tls: {}
    # secretName: my-tls-secret
    # options:
    #   name: my-tls-options
```

## Guardrails
- **Native Ingress**: Do NOT generate native `Networking/v1 Ingress` unless explicitly required. Favor the `traefik.io/v1alpha1 IngressRoute`.
- **Middleware Prefixing**: Middlewares are automatically prefixed with the chart fullname to avoid cluster-wide naming collisions. When attaching them via `hosts.middlewares`, the user must provide the *full name*.
- **EntryPoints**: Traefik requires explicit entrypoints. Defaulting to `["web", "websecure"]` for HTTP ensures backwards compatibility.
