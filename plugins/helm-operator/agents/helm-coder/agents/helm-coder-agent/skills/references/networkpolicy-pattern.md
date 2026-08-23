# NetworkPolicy Pattern

When generating `templates/networkpolicy.yaml`, implement a deny-all-by-default policy
with explicit ingress/egress allowlists.

## Template Code Standard

```yaml
{{- if .Values.networkPolicy.enabled }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "my-chart.fullname" . }}
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "my-chart.selectorLabels" . | nindent 6 }}
  policyTypes:
    - Ingress
    - Egress
  ingress:
    {{- if .Values.networkPolicy.allowExternal }}
    - {}
    {{- else }}
    - from:
        - podSelector:
            matchLabels:
              {{ include "my-chart.fullname" . }}-client: "true"
        {{- if .Values.networkPolicy.ingressNSMatchLabels }}
        - namespaceSelector:
            matchLabels:
              {{- toYaml .Values.networkPolicy.ingressNSMatchLabels | nindent 14 }}
        {{- end }}
      ports:
        - port: {{ .Values.service.port }}
          protocol: TCP
    {{- end }}
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
    {{- with .Values.networkPolicy.egressRules }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
{{- end }}
```

## values.yaml Keys

```yaml
networkPolicy:
  enabled: false
  allowExternal: false
  ingressNSMatchLabels: {}
  egressRules: []
```

## Guardrails
- Always allow DNS egress (port 53 UDP/TCP) or pods cannot resolve service names.
- Use `podSelector.matchLabels` with selectorLabels (not labels) to target only this app's pods.
- The `allowExternal: true` escape hatch opens all ingress — use only for public-facing services.
