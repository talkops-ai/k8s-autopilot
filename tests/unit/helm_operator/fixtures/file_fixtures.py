def get_workspace_files_nginx():
    """Simulated virtual FS output from helm-generator."""
    return {
        "/workspace/helm-charts/nginx/Chart.yaml": {"content": "apiVersion: v2\nname: nginx"},
        "/workspace/helm-charts/nginx/values.yaml": {"content": "replicaCount: 1"},
        "/workspace/helm-charts/nginx/templates/deployment.yaml": {"content": "---"},
    }

def get_empty_helm_files():
    return {}
