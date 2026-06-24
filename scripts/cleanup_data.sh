#!/bin/bash
set -e

NAMESPACE="monitoring"

echo "🧹 Starting cleanup of Prometheus, Loki, and Tempo data in namespace: $NAMESPACE"

echo "📦 Scaling down Loki and Tempo statefulsets to release PVCs..."
kubectl scale sts/loki --replicas=0 -n "$NAMESPACE" || true
kubectl scale sts/tempo --replicas=0 -n "$NAMESPACE" || true

echo "⏳ Waiting for Loki and Tempo pods to terminate..."
kubectl wait --for=delete pod -l app.kubernetes.io/name=loki -n "$NAMESPACE" --timeout=60s || true
kubectl wait --for=delete pod -l app.kubernetes.io/name=tempo -n "$NAMESPACE" --timeout=60s || true

echo "🗑️ Deleting Loki and Tempo PVCs to wipe persistent data..."
kubectl delete pvc -l app.kubernetes.io/name=loki -n "$NAMESPACE" --ignore-not-found
kubectl delete pvc -l app.kubernetes.io/name=tempo -n "$NAMESPACE" --ignore-not-found
kubectl delete pvc storage-loki-0 -n "$NAMESPACE" --ignore-not-found
kubectl delete pvc storage-tempo-0 -n "$NAMESPACE" --ignore-not-found

echo "🚀 Scaling Loki and Tempo back up to recreate fresh PVCs..."
kubectl scale sts/loki --replicas=1 -n "$NAMESPACE" || true
kubectl scale sts/tempo --replicas=1 -n "$NAMESPACE" || true

echo "🔄 Restarting Prometheus pod(s) to clear memory/emptyDir storage..."
# Prometheus is managed by prometheus-operator, so deleting the pod causes it to restart with clean emptyDir
kubectl delete pod -l app.kubernetes.io/name=prometheus -n "$NAMESPACE" --ignore-not-found

echo "✅ Cleanup complete! Fresh pods and volumes are being provisioned."
