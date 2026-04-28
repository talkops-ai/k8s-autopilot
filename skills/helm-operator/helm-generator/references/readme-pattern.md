# Helm Chart README Pattern

Every generated Helm chart MUST include a comprehensive, professional `README.md` file that matches the quality standards of industry-leading charts (such as Bitnami Helm charts). The README is the primary entry point for users to understand, install, and configure the application.

## 1. Document Structure

The generated `README.md` must follow this structure exactly:

1. **Title and Badges**: The name of the chart and basic metadata.
2. **Introduction**: A concise description of the application and the purpose of the chart.
3. **TL;DR**: The quickest way to install the chart using default settings.
4. **Prerequisites**: Minimum K8s version, required CRDs (e.g., Traefik), and dependencies.
5. **Installing the Chart**: Step-by-step instructions on how to install.
6. **Uninstalling the Chart**: Step-by-step instructions on how to remove it.
7. **Configuration Parameters**: A comprehensive table listing all options in `values.yaml`.

## 2. Go Template Pattern for README Generation

Since the README is generated via the LLM, you should output standard Markdown text. Ensure you fill in the placeholder values appropriately.

### Title and Introduction
```markdown
# [Chart Name]

[A 1-2 sentence description of the application. Extract this from the execution-blueprint.md or the user's intent.]

This Helm chart bootstraps a [Chart Name] deployment on a [Kubernetes](http://kubernetes.io) cluster.
```

### TL;DR
Provide the simplest commands to install the chart assuming it is in the current directory:
```markdown
## TL;DR

```bash
helm install my-release .
```
```

### Prerequisites
Include generic Kubernetes prerequisites, plus any specifics required by the chart's architecture (like Traefik for IngressRoute).
```markdown
## Prerequisites

- Kubernetes 1.19+
- Helm 3.2.0+
- PV provisioner support in the underlying infrastructure (if persistence is enabled)
- [Traefik Ingress Controller](https://doc.traefik.io/traefik/) (if IngressRoute is enabled)
```

### Installing the Chart
```markdown
## Installing the Chart

To install the chart with the release name `my-release`:

```bash
helm install my-release .
```

The command deploys [Chart Name] on the Kubernetes cluster in the default configuration. The [Parameters](#parameters) section lists the parameters that can be configured during installation.
```

### Uninstalling the Chart
```markdown
## Uninstalling the Chart

To uninstall/delete the `my-release` deployment:

```bash
helm uninstall my-release
```

The command removes all the Kubernetes components associated with the chart and deletes the release.
```

### Configuration Parameters Table
This is the most critical section. You MUST document all major parameters defined in `values.yaml`, especially under `image`, `service`, `ingress`, `resources`, `autoscaling`, and `securityContext`.

Use a Markdown table with the following headers:
- Parameter
- Description
- Default

Example:

```markdown
## Parameters

### Generic parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of replicas | `1` |
| `image.repository` | Image repository | `nginx` |
| `image.tag` | Image tag | `latest` |
| `imagePullSecrets` | Specify image pull secrets | `[]` |
| `nameOverride` | String to partially override release name | `""` |
| `fullnameOverride`| String to fully override release name | `""` |

### Service parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `service.type` | Kubernetes Service type | `ClusterIP` |
| `service.port` | Service HTTP port | `80` |

### Ingress parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ingress.enabled` | Enable Traefik IngressRoute | `false` |
| `ingress.hosts[0].host` | Hostname to your application | `chart-example.local` |
| `ingress.entryPoints` | Traefik entry points | `["web"]` |

### Resource parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `resources.limits.cpu` | CPU limits | `500m` |
| `resources.limits.memory` | Memory limits | `512Mi` |
| `resources.requests.cpu` | CPU requests | `250m` |
| `resources.requests.memory`| Memory requests | `256Mi` |

```

## Guardrails
- **Completeness:** Ensure that the generated README actually corresponds to the templates you are generating. Don't document features that aren't in the templates.
- **Accuracy:** The exact default values must match what is written in `values.yaml`.
- **Formatting:** Ensure tables are properly aligned and Markdown is valid.
