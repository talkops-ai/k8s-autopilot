# Chart Standards & Best Practices

This document establishes the mandatory conventions and functional best practices for generating and structuring Helm charts.

## Core Directives
- **Strict Versioning**: Declare modern K8s API versions (e.g., `networking.k8s.io/v1` for `Ingress`, `apps/v1` for `Deployment`).
- **Idempotence**: Do NOT include objects like `Job` (run-once) without appropriate hook annotations (e.g., `helm.sh/hook: post-install`).
- **Validation**: Ensure templates pass `helm lint` successfully before committing.

## Architecture
- **Official Structure**: Always maintain `Chart.yaml` (name, version, apiVersion: v2), `values.yaml`, `templates/`, and `_helpers.tpl`.
- **Dynamic Values**: Avoid hardcoding configurations in `templates/`. Use Go templates (e.g., `{{ .Values.replicaCount }}`).
- **Configurations**: Always provision `ConfigMap` templates if application configuration exists, utilizing graceful `{}` fallbacks.
- **Resource Definitions**: Provide explicit defaults for Resource Requests & Limits in `values.yaml` (e.g., CPU 100m, Memory 128Mi). Do not leave resources naked.

## Standard Labels
All charts must dynamically apply standard Kubernetes labels to ensure robust management mapping.

**Required in `templates/_helpers.tpl`:**
```yaml
{{- define "chart.labels" -}}
helm.sh/chart: {{ include "chart.chart" . }}
{{ include "chart.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
talkops.ai/generated: "true"
{{- end }}

{{- define "chart.selectorLabels" -}}
app.kubernetes.io/name: {{ include "chart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```
