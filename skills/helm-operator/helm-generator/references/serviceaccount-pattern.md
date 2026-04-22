# ServiceAccount Pattern

When generating `templates/serviceaccount.yaml`, create a dedicated SA per
release with opt-out token mounting.

## Template Code Standard

```yaml
{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "my-chart.serviceAccountName" . }}
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
  {{- with .Values.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountToken | default false }}
{{- end }}
```

## values.yaml Keys

```yaml
serviceAccount:
  create: true
  ## Name override. If empty, uses {{ include "my-chart.fullname" . }}
  name: ""
  ## Annotations for IAM roles (e.g., eks.amazonaws.com/role-arn)
  annotations: {}
  ## Disable auto-mounting unless the pod explicitly needs K8s API access
  automountToken: false
```

## _helpers.tpl Dependency

The ServiceAccount template relies on this helper (already in `helpers-and-values.md`):

```gotemplate
{{- define "my-chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "my-chart.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
```

## Guardrails
- Always set `automountServiceAccountToken: false` by default. Only enable when the pod needs K8s API access.
- The Deployment template MUST reference `serviceAccountName: {{ include "my-chart.serviceAccountName" . }}`.
- For EKS IRSA, annotations must include `eks.amazonaws.com/role-arn`.
