# Helm Hooks Pattern

When generating `templates/hooks/*.yaml`, use Helm lifecycle hooks for
pre/post-install and pre/post-upgrade jobs (e.g., database migrations).

## Template Code Standard — Pre-Install/Upgrade Job

```yaml
{{- if .Values.hooks.migration.enabled }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "my-chart.fullname" . }}-migrate
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "1"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: {{ .Values.hooks.migration.backoffLimit | default 3 }}
  template:
    metadata:
      labels:
        {{- include "my-chart.selectorLabels" . | nindent 8 }}
    spec:
      restartPolicy: Never
      serviceAccountName: {{ include "my-chart.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.podSecurityContext | nindent 8 }}
      containers:
        - name: migrate
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            {{- toYaml .Values.hooks.migration.command | nindent 12 }}
          env:
            {{- toYaml .Values.hooks.migration.env | nindent 12 }}
          resources:
            {{- toYaml .Values.hooks.migration.resources | nindent 12 }}
          securityContext:
            {{- toYaml .Values.securityContext | nindent 12 }}
{{- end }}
```

## values.yaml Keys

```yaml
hooks:
  migration:
    enabled: false
    command: ["python", "manage.py", "migrate"]
    env: []
    backoffLimit: 3
    resources:
      limits:
        cpu: 250m
        memory: 256Mi
      requests:
        cpu: 100m
        memory: 128Mi
```

## Hook Weight Ordering

When multiple hooks run at the same lifecycle event, use `hook-weight` to control order:

| Weight | Purpose |
|---|---|
| `-5` | Wait-for-dependency checks (e.g., wait-for-db) |
| `0` | Schema migrations |
| `5` | Data seeding |
| `10` | Cache warming |

## Guardrails
- Always set `helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded` to clean up completed jobs.
- Use `restartPolicy: Never` for Jobs — `OnFailure` can mask errors.
- Hook Jobs MUST have `resources.limits` — unbounded hook jobs can starve cluster resources.
- Always include `securityContext` — hooks run in the same namespace and should follow the same security posture.
- The hook container image should use the SAME image as the main app (not a separate utility image) for migration jobs.
