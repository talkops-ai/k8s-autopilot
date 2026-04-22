# Traefik L4 Routing: TCP & UDP IngressRoutes

This reference defines configuration for arbitrary Layer 4 routing (TCP/UDP) using Traefik. Native Kubernetes `Ingress` only supports HTTP/HTTPS, making `IngressRouteTCP` and `IngressRouteUDP` essential for routing databases, MQTT brokers, Redis, or custom binary protocols.

Use these patterns when the user requests exposing non-HTTP workloads.

## 1. TCP IngressRoute (IngressRouteTCP)
Used for TCP traffic. Can be used in "Passthrough" mode (where the backend handles TLS termination mapping to SNI) or purely raw TCP matching `"*"` if the port is exclusively assigned.

**Common Use Case**: Exposing a Postgres Database or MQTT broker on a specific port.

```yaml
{{- if and .Values.ingress.enabled (eq .Values.ingress.protocol "tcp") -}}
apiVersion: traefik.io/v1alpha1
kind: IngressRouteTCP
metadata:
  name: {{ include "my-chart.fullname" . }}-tcp
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
spec:
  entryPoints:
    # E.g., ["mysql", "mqtt"]
    {{- toYaml (default (list "tcp") .Values.ingress.entryPoints) | nindent 4 }}
  routes:
    {{- range .Values.ingress.tcpHosts }}
    - match: HostSNI(`{{ default "*" .sni }}`)
      services:
        - name: {{ include "my-chart.fullname" $ }}
          port: {{ $.Values.service.port }}
          {{- if $.Values.ingress.proxyProtocol }}
          proxyProtocol:
            version: {{ $.Values.ingress.proxyProtocol.version | default 2 }}
          {{- end }}
    {{- end }}
  {{- if .Values.ingress.tls }}
  tls:
    # If using SNI but the backend terminates TLS, set passthrough: true
    passthrough: {{ default false .Values.ingress.tls.passthrough }}
    {{- if .Values.ingress.tls.secretName }}
    secretName: {{ .Values.ingress.tls.secretName }}
    {{- end }}
  {{- end }}
{{- end }}
```

## 2. UDP IngressRoute (IngressRouteUDP)
Used for stateless UDP traffic. UDP does not support TLS or SNI matching natively within Traefik routing rules, so it matches purely by the EntryPoint.

**Common Use Case**: Exposing DNS proxies, game servers, or statsd metrics collectors.

```yaml
{{- if and .Values.ingress.enabled (eq .Values.ingress.protocol "udp") -}}
apiVersion: traefik.io/v1alpha1
kind: IngressRouteUDP
metadata:
  name: {{ include "my-chart.fullname" . }}-udp
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
spec:
  entryPoints:
    # E.g., ["dns", "statsd"]
    {{- toYaml (default (list "udp") .Values.ingress.entryPoints) | nindent 4 }}
  routes:
    - services:
        - name: {{ include "my-chart.fullname" . }}
          port: {{ .Values.service.port }}
{{- end }}
```

## values.yaml Keys

```yaml
ingress:
  enabled: false
  ## "tcp" for IngressRouteTCP, "udp" for IngressRouteUDP
  protocol: tcp
  
  ## Traefik entrypoints (MUST be pre-configured on Traefik itself)
  entryPoints: ["tcp"]
  
  ## TCP Routing (protocol: tcp)
  tcpHosts:
    - sni: "*" # Use "*" for raw TCP without TLS, or a specific SNI hostname

  proxyProtocol: {}
    # version: 2

  ## TLS Configuration (TCP only)
  tls: {}
    # passThrough: false
    # secretName: my-tls-secret
```

## Guardrails
- **HostSNI**: `IngressRouteTCP` uses `HostSNI()`. If `tcpHosts[0].sni == "*"` (catch-all TCP), TLS should be omitted or passthrough cannot be enabled unless global default certs exist.
- **Dedicated L4 Entrypoints**: Traefik requires strict separation of HTTP and raw TCP/UDP entrypoints. If forwarding raw TCP/UDP, ensure the user provides an `entryPoint` name that exists and is designated for L4 traffic on the cluster proxy instance.
- **UDP Matchers**: `IngressRouteUDP` does NOT have a `match` block. Do not attempt to add `match: Host()` to UDP routes.
