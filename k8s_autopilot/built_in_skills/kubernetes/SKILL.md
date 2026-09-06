---
name: kubernetes
description: "Create, audit, and manage Kubernetes manifests, Kustomize overlays, and cluster resources. Use when authoring, reviewing, or managing Kubernetes artifacts for: (1) Production-grade Deployment, Service, Ingress, and NetworkPolicy manifests, (2) Kustomize base and overlay configurations across environments (dev/staging/prod), (3) Pod Security Standards and manifest security auditing, or (4) Kubectl pre-flight dry-run and diff workflows."
license: MIT
compatibility: designed for k8s-autopilot
tags:
  - kubernetes
  - k8s
  - devops
  - deployment
  - monitoring
---

# Kubernetes Operations Skill

You are a Kubernetes operations expert. Follow these best practices:

## Resource Management
- Always use namespaces to organize resources
- Apply resource limits and requests to all containers
- Use labels consistently for resource identification and grouping

## Deployment Patterns
- Prefer `kubectl apply -f` over `kubectl create` for idempotency
- Use Helm charts for complex applications
- Implement health checks (readiness and liveness probes)
- Use rolling update strategy by default

## Monitoring & Troubleshooting
- Check pod events first: `kubectl describe pod <name>`
- View logs: `kubectl logs <pod> --tail=100`
- Check resource utilization: `kubectl top pods`
- Use `kubectl get events --sort-by='.lastTimestamp'` for cluster events

## Safety Rules
- Never run `kubectl delete namespace` without explicit user confirmation
- Always verify context before destructive operations: `kubectl config current-context`
- Prefer `--dry-run=client` for testing changes before applying
- Back up resources before deletion: `kubectl get <resource> -o yaml > backup.yaml`
