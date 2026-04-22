# NOTES.txt Pattern

`templates/NOTES.txt` is rendered after `helm install` and `helm upgrade`
to display post-install instructions to the user.

## Template Code Standard

```
{{- $fullName := include "my-chart.fullname" . -}}
{{- $svcPort := .Values.service.port -}}

1. Get the application URL by running these commands:
{{- if .Values.ingress.enabled }}
{{- range .Values.ingress.hosts }}
  http{{ if $.Values.ingress.tls }}s{{ end }}://{{ .host }}
{{- end }}
{{- else if contains "NodePort" .Values.service.type }}
  export NODE_PORT=$(kubectl get --namespace {{ .Release.Namespace }} -o jsonpath="{.spec.ports[0].nodePort}" services {{ $fullName }})
  export NODE_IP=$(kubectl get nodes --namespace {{ .Release.Namespace }} -o jsonpath="{.items[0].status.addresses[0].address}")
  echo http://$NODE_IP:$NODE_PORT
{{- else if contains "LoadBalancer" .Values.service.type }}
     NOTE: It may take a few minutes for the LoadBalancer IP to be available.
           You can watch the status by running:
           kubectl get --namespace {{ .Release.Namespace }} svc -w {{ $fullName }}
  export SERVICE_IP=$(kubectl get svc --namespace {{ .Release.Namespace }} {{ $fullName }} --template "{{"{{ range (index .status.loadBalancer.ingress 0) }}{{.}}{{ end }}"}}")
  echo http://$SERVICE_IP:{{ $svcPort }}
{{- else }}
  export POD_NAME=$(kubectl get pods --namespace {{ .Release.Namespace }} -l "{{ include "my-chart.selectorLabels" . | replace "\n" "," }}" -o jsonpath="{.items[0].metadata.name}")
  export CONTAINER_PORT=$(kubectl get pod --namespace {{ .Release.Namespace }} $POD_NAME -o jsonpath="{.spec.containers[0].ports[0].containerPort}")
  echo "Visit http://127.0.0.1:8080 to use your application"
  kubectl --namespace {{ .Release.Namespace }} port-forward $POD_NAME 8080:$CONTAINER_PORT
{{- end }}
```

## Customization Points

Add any of these sections based on the chart's features:

### Subchart Dependencies
```
2. If this is a fresh install, run dependency update first:
  helm dependency update ./{{ .Chart.Name }}
```

### Health Check
```
2. Verify the deployment is healthy:
  kubectl get pods --namespace {{ .Release.Namespace }} -l "app.kubernetes.io/name={{ include "my-chart.name" . }},app.kubernetes.io/instance={{ .Release.Name }}"
```

## Guardrails
- Always use `{{ include "my-chart.fullname" . }}` for the release name — not `{{ .Release.Name }}` directly.
- Always use `{{ .Release.Namespace }}` — never hardcode a namespace.
- Use `$fullName` variable to avoid repeating the include call.
- Handle all 4 service types: Ingress, NodePort, LoadBalancer, ClusterIP (port-forward).
