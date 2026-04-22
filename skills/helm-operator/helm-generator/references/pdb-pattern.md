# PodDisruptionBudget Pattern

When generating `templates/pdb.yaml`, ensure high-availability pods survive
voluntary disruptions (node drains, upgrades).

## Template Code Standard

```yaml
{{- if .Values.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "my-chart.fullname" . }}
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
spec:
  {{- if .Values.pdb.minAvailable }}
  minAvailable: {{ .Values.pdb.minAvailable }}
  {{- else }}
  maxUnavailable: {{ .Values.pdb.maxUnavailable | default 1 }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "my-chart.selectorLabels" . | nindent 6 }}
{{- end }}
```

## values.yaml Keys

```yaml
pdb:
  enabled: false
  ## Only ONE of minAvailable or maxUnavailable should be set — never both
  # minAvailable: 1
  maxUnavailable: 1
```

## Guardrails
- Never set both `minAvailable` AND `maxUnavailable`. Kubernetes rejects the resource.
- Prefer `maxUnavailable: 1` for most workloads — it allows at most 1 pod to be unavailable during rolling updates.
- Use `minAvailable` only when you need an absolute floor (e.g., quorum-based systems).
- Use `policy/v1` (not `policy/v1beta1`) for Kubernetes >= 1.21.
