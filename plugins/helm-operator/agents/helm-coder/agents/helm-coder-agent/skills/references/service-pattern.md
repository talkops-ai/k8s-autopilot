# Service Pattern

When generating `templates/service.yaml`, map the internal Pod ports seamlessly.

## Template Code Standard

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "my-chart.fullname" . }}
  labels:
    {{- include "my-chart.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: http
      protocol: TCP
      name: http
  selector:
    {{- include "my-chart.selectorLabels" . | nindent 4 }}
```

## Guardrails
- The `targetPort` must usually match the Named Port on the deployment's container.
- Always bind `type` dynamically from `Values` so users can overwrite with `NodePort` or `LoadBalancer` if manually requested!
