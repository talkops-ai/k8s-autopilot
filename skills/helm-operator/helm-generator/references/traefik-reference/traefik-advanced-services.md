# Traefik Advanced Services: Shadowing, Canary, & Failover

This reference defines Traefik's advanced `loadBalancer` service types. Instead of routing traffic directly to Kubernetes Services, these constructs compose multiple services together to enable advanced traffic distribution schemas. 

Use these patterns when the user requests canary deployments, blue/green rollouts, traffic mirroring, or multi-cluster failover.

## Creating the CRD

Unlike Middlewares, there is no discrete `traefik.io/v1alpha1 Service` CRD that holds these definitions. These advanced routing capabilities are defined directly within the `IngressRoute` itself by specifying a `kind: TraefikService` and referencing the service via provider definitions, OR by defining them in a dedicated `TraefikService` CRD.

For Helm Generator purposes, we define them via the `traefik.io/v1alpha1 TraefikService` Custom Resource Definition.

```yaml
{{- if .Values.ingress.enabled }}
{{- range $name, $config := .Values.ingress.customServices }}
---
apiVersion: traefik.io/v1alpha1
kind: TraefikService
metadata:
  name: {{ include "my-chart.fullname" $ }}-{{ $name }}
spec:
  {{- toYaml $config | nindent 2 }}
{{- end }}
{{- end }}
```

Which allows the `IngressRoute` to route traffic to them:

```yaml
  routes:
    - match: Host(`example.com`)
      kind: Rule
      services:
        # Note the kind is TraefikService, not Service (which defaults to Kubernetes Service)
        - name: {{ include "my-chart.fullname" $ }}-weighted-canary
          kind: TraefikService
```

## 1. Weighted Round Robin (Canary)

Splits traffic between two or more Kubernetes Services using proportional weights.

**Common Use Case**: Canary deployments (sending 10% of traffic to a new version `v2`, 90% to `v1`).

```yaml
# values.yaml example
ingress:
  customServices:
    weighted-canary:
      weighted:
        services:
          - name: my-chart-app-v1
            kind: Service
            port: 80
            weight: 9
          - name: my-chart-app-v2
            kind: Service
            port: 80
            weight: 1
```

## 2. Mirroring (Traffic Shadowing)

Mirrors requests sent to a primary service to another service in a Fire-and-Forget manner. The mirrored request's response is ignored.

**Common Use Case**: Testing a new version of an application with real production traffic without impacting clients.

```yaml
# values.yaml example
ingress:
  customServices:
    shadow-mirror:
      mirroring:
        # Main service serving traffic
        name: my-chart-app-v1
        port: 80
        # Shadow service receiving percentage
        mirrors:
          - name: my-chart-app-v2
            port: 80
            percent: 10 # 10% representation of traffic mirrored
```

## 3. Failover
Immediately routes all traffic to a fallback service if the primary service becomes unhealthy.

**Common Use Case**: High availability across zones. Routing to a static "Maintenance Page" service if the primary API goes down.

> **Pre-requisite**: The primary (main) service must have health checks configured in Traefik.

```yaml
# values.yaml example
ingress:
  customServices:
    ha-failover:
      failover:
        service: 
          name: my-chart-main-api
          port: 80
        fallback: 
          name: my-chart-maintenance-page
          port: 80
```
