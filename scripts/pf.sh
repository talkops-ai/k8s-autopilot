#!/bin/bash

NAMESPACE="monitoring"

start_forwards() {
    echo "Starting port forwards for monitoring services..."
    echo "Logs will be displayed below. Press Ctrl+C to stop all port forwards."
    echo "-------------------------------------------------------------------"
    
    PIDS=()

    # Start each port-forward and capture its Process ID (PID)
    kubectl port-forward svc/tempo -n $NAMESPACE 3200:3200 &
    PIDS+=($!)

    kubectl port-forward svc/loki -n $NAMESPACE 3100:3100 &
    PIDS+=($!)

    kubectl port-forward svc/prometheus-kube-prometheus-alertmanager -n $NAMESPACE 9095:9093 &
    PIDS+=($!)

    kubectl port-forward svc/prometheus-kube-prometheus-prometheus -n $NAMESPACE 9091:9090 &
    PIDS+=($!)

    echo "Starting port forwards for other services..."
    kubectl port-forward svc/argo-cd-argocd-server -n argo-cd 8080:80 &
    PIDS+=($!)

    kubectl port-forward svc/argo-rollouts-dashboard -n argo-rollouts 3101:3100 &
    PIDS+=($!)

    kubectl port-forward deploy/traefik -n traefik 9000:8080 &
    PIDS+=($!)

    # Setup a trap to gracefully kill all PIDs when you press Ctrl+C
    trap 'echo -e "\n🛑 Stopping monitoring port forwards..."; kill ${PIDS[@]} 2>/dev/null; exit 0' SIGINT SIGTERM

    # Wait indefinitely while the port forwards run
    wait
}

stop_forwards() {
    echo "Manually stopping any detached monitoring port forwards..."
    
    pkill -f "kubectl port-forward svc/tempo -n $NAMESPACE 3200:3200"
    pkill -f "kubectl port-forward svc/loki -n $NAMESPACE 3100:3100"
    pkill -f "kubectl port-forward svc/prometheus-kube-prometheus-alertmanager -n $NAMESPACE 9095:9093"
    pkill -f "kubectl port-forward svc/prometheus-kube-prometheus-prometheus -n $NAMESPACE 9091:9090"
    pkill -f "kubectl port-forward svc/argo-cd-argocd-server -n argo-cd 8080:80"
    pkill -f "kubectl port-forward svc/argo-rollouts-dashboard -n argo-rollouts 3101:3100"
    pkill -f "kubectl port-forward deploy/traefik -n traefik 9000:8080"
    
    echo "🛑 Port forwards stopped."
}

case "$1" in
    start)
        start_forwards
        ;;
    stop)
        stop_forwards
        ;;
    *)
        echo "Usage: $0 {start|stop}"
        echo "  start : Run port forwards in the foreground and monitor logs"
        echo "  stop  : Kill any detached port forwards manually"
        exit 1
esac