# Core Template Generation Tools

This document describes the core tools that are **always executed** during Helm chart generation.

## Table of Contents

1. [generate_helpers_tpl](#generate_helperstpl)
2. [generate_namespace_yaml](#generate_namespaceyaml)
3. [generate_deployment_yaml](#generate_deploymentyaml)
4. [generate_service_yaml](#generate_serviceyaml)
5. [generate_values_yaml](#generate_valuesyaml)

---

## generate_helpers_tpl

**Purpose**: Generate Helm helper templates (`_helpers.tpl`) with standard template functions.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/helper/helper_tool.py`

**Execution Order**: **FIRST** - Must execute before all other templates (other templates reference helpers)

**Dependencies**: None

### Input

Extracts from state:
- `planner_output.parsed_requirements.app_name`
- `planner_output.parsed_requirements.chart_name` (or defaults to app_name)

### Output Schema

```python
class HelpersTplGenerationOutput(BaseModel):
    tpl_content: str  # Content of _helpers.tpl
    file_name: str = "_helpers.tpl"
    defined_templates: List[str]  # List of defined template names
    validation_messages: List[str] = []
```

### Generated Templates

The tool generates these standard Helm helper templates:

1. **`{{- define "CHARTNAME.name" }}`** - Chart name
2. **`{{- define "CHARTNAME.fullname" }}`** - Full resource name (release + chart)
3. **`{{- define "CHARTNAME.chart" }}`** - Chart label (name-version)
4. **`{{- define "CHARTNAME.labels" }}`** - Standard labels
5. **`{{- define "CHARTNAME.selectorLabels" }}`** - Selector labels
6. **`{{- define "CHARTNAME.serviceAccountName" }}`** - ServiceAccount name with conditional logic

### Example Output

```yaml
{{/*
Expand the name of the chart.
*/}}
{{- define "myapp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "myapp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "myapp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "myapp.labels" -}}
helm.sh/chart: {{ include "myapp.chart" . }}
{{ include "myapp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "myapp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "myapp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "myapp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "myapp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
```

### Usage in Other Templates

Other templates reference helpers like this:

```yaml
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels:
      {{- include "myapp.selectorLabels" . | nindent 6 }}
```

---

## generate_namespace_yaml

**Purpose**: Generate Namespace resource YAML.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/namespace/namespace_tool.py`

**Execution Order**: After `generate_helpers_tpl`, before `generate_deployment_yaml` (if Namespace exists)

**Dependencies**: `["generate_helpers_tpl"]`

### Conditional Execution

Only executed if:
- `planner_output.kubernetes_architecture.resources.core` contains a resource with `type == "Namespace"`

### Input

Extracts from state:
- `planner_output.parsed_requirements.namespace.name`
- `planner_output.parsed_requirements.namespace.namespace_type`
- `planner_output.parsed_requirements.namespace.team`

### Output Schema

```python
class NamespaceGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "namespace.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

### Example Output

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: {{ .Values.namespace.name }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
    {{- with .Values.namespace.labels }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  annotations:
    {{- with .Values.namespace.annotations }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
```

### Key Features

- Uses helper templates for labels
- Supports custom labels and annotations via values.yaml
- Namespace name is templated for flexibility

---

## generate_deployment_yaml

**Purpose**: Generate Deployment or StatefulSet YAML manifest.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/deployment/deployment_tool.py`

**Execution Order**: After helpers (and namespace if exists), before service

**Dependencies**: 
- `["generate_helpers_tpl"]` (always)
- `["generate_helpers_tpl", "generate_namespace_yaml"]` (if namespace exists)

### Input Schema

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/deployment/deployment_tool.py`

**Key Fields**:
- `app_name`: Application name
- `workload_type`: "Deployment" or "StatefulSet"
- `image`: Container image with tag
- `replicas`: Number of replicas
- `resources`: CPU/memory requests and limits
- `namespace`: Target namespace
- `liveness_probe`: Liveness probe configuration
- `readiness_probe`: Readiness probe configuration
- `security_context`: Security context settings
- `environment_variables`: List of environment variables
- `volume_mounts`: Volume mount specifications
- `volumes`: Volume definitions

### Output Schema

```python
class DeploymentGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str  # "deployment.yaml" or "statefulset.yaml"
    template_variables_used: List[str]
    helm_template_functions_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
    kubernetes_api_version: str = "apps/v1"
    generated_resources: List[str]
    security_features_applied: List[str]
```

### Key Features

1. **Security Hardening**:
   - `runAsNonRoot: true`
   - `readOnlyRootFilesystem` (configurable)
   - `allowPrivilegeEscalation: false`
   - Drop ALL capabilities
   - Seccomp profile

2. **Health Probes**:
   - Liveness probe (from framework analysis)
   - Readiness probe (from framework analysis)
   - Startup probe (for slow-starting containers)

3. **Resource Management**:
   - CPU/memory requests and limits
   - QoS class (Guaranteed/Burstable/BestEffort)

4. **Helm Templating**:
   - All values use `{{ .Values.* }}`
   - Labels use helper templates
   - Selectors use helper templates

### Example Output Structure

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "myapp.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "myapp.selectorLabels" . | nindent 8 }}
    spec:
      securityContext:
        runAsNonRoot: {{ .Values.securityContext.runAsNonRoot }}
        runAsUser: {{ .Values.securityContext.runAsUser }}
        seccompProfile:
          type: {{ .Values.securityContext.seccompProfile.type }}
      containers:
      - name: {{ .Chart.Name }}
        image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
        imagePullPolicy: {{ .Values.image.pullPolicy }}
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: {{ .Values.securityContext.readOnlyRootFilesystem }}
          capabilities:
            drop:
            - ALL
        resources:
          requests:
            memory: {{ .Values.resources.requests.memory }}
            cpu: {{ .Values.resources.requests.cpu }}
          limits:
            memory: {{ .Values.resources.limits.memory }}
            cpu: {{ .Values.resources.limits.cpu }}
        livenessProbe:
          httpGet:
            path: {{ .Values.livenessProbe.path }}
            port: {{ .Values.livenessProbe.port }}
          initialDelaySeconds: {{ .Values.livenessProbe.initialDelaySeconds }}
          periodSeconds: {{ .Values.livenessProbe.periodSeconds }}
        readinessProbe:
          httpGet:
            path: {{ .Values.readinessProbe.path }}
            port: {{ .Values.readinessProbe.port }}
          initialDelaySeconds: {{ .Values.readinessProbe.initialDelaySeconds }}
          periodSeconds: {{ .Values.readinessProbe.periodSeconds }}
        env:
        {{- range .Values.env }}
        - name: {{ .name }}
          value: {{ .value | quote }}
        {{- end }}
```

### Data Mapping from Planner

| Planner Path | Tool Input Field |
|--------------|------------------|
| `parsed_requirements.app_name` | `app_name` |
| `kubernetes_architecture.resources.core[0].type` | `workload_type` |
| `parsed_requirements.image.full_image` | `image` |
| `parsed_requirements.deployment.min_replicas` | `replicas` |
| `resource_estimation.prod` | `resources` |
| `application_analysis.security` | `security_context` |
| `application_analysis.framework_analysis` | `liveness_probe`, `readiness_probe` |
| `parsed_requirements.configuration.environment_variables` | `environment_variables` |

---

## generate_service_yaml

**Purpose**: Generate Service manifest for service discovery.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/service/service_tool.py`

**Execution Order**: After deployment (needs deployment labels for selectors)

**Dependencies**: `["generate_deployment_yaml"]`

### Input

Extracts from state:
- `planner_output.parsed_requirements.service.access_type`
- `planner_output.parsed_requirements.service.port`
- `planner_output.parsed_requirements.service.target_port`
- `planner_output.application_analysis.networking.port`

### Output Schema

```python
class ServiceGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "service.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    service_endpoints: List[str]  # Expected endpoints
    metadata: Dict[str, Any] = {}
    kubernetes_api_version: str = "v1"
    generated_resources: List[str] = ["Service"]
    validation_messages: List[str] = []
    helm_template_functions_used: List[str] = []
```

### Service Types Supported

1. **ClusterIP** (default) - Internal cluster access
2. **LoadBalancer** - External access via cloud load balancer
3. **NodePort** - External access via node IPs
4. **Headless** (ClusterIP: None) - For StatefulSets

### Key Features

1. **Selector Matching**: Service selector MUST match Deployment pod labels exactly
2. **Port Naming**: Ports are named (required for Istio/Linkerd)
3. **Session Affinity**: Configurable for stateful apps
4. **Helm Templating**: All values templated

### Example Output

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: {{ .Values.service.targetPort }}
      protocol: TCP
      name: http
  selector:
    {{- include "myapp.selectorLabels" . | nindent 4 }}
  {{- if eq .Values.service.type "LoadBalancer" }}
  loadBalancerIP: {{ .Values.service.loadBalancerIP | default "" }}
  externalTrafficPolicy: {{ .Values.service.externalTrafficPolicy }}
  {{- end }}
```

### Critical Requirement

**Selector Matching**: The Service selector must match Deployment pod labels exactly. Both use `{{- include "myapp.selectorLabels" . }}` to ensure consistency.

---

## generate_values_yaml

**Purpose**: Generate comprehensive `values.yaml` file that parameterizes all generated templates.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/values/values_tool.py`

**Execution Order**: **AFTER** all templates (core + conditional) - collects all template variables

**Dependencies**: ALL templates (core + conditional)

### Why This Order Matters

`values.yaml` must be generated **after** all templates because:
1. It needs to collect all `{{ .Values.* }}` references from generated templates
2. It ensures complete coverage of all template variables
3. It provides defaults based on planner output

### Input

Extracts from state:
- `planner_output` (complete planner output)
- `generated_templates` (all generated YAML templates)
- `template_variables` (accumulated list of all `{{ .Values.* }}` references)

### Output Schema

```python
class ValuesYamlGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "values.yaml"
    sections: List[ValuesSection]  # Structured sections
    schema_definition: Dict[str, Any]  # JSON Schema for validation
    coverage_percentage: float  # % of template vars covered (must be >= 95%)
    metadata: Dict[str, Any] = {}
```

### Structure

The generated `values.yaml` follows Bitnami-style format with inline documentation:

```yaml
# Default values for myapp.
# This is a YAML-formatted file.
# Declare variables to be passed into your templates.

## @section Image parameters
## Container image configuration

image:
  ## @param image.repository Container registry/repository
  ## @default sandeep2014/aws-orchestrator-agent
  repository: sandeep2014/aws-orchestrator-agent
  
  ## @param image.pullPolicy Image pull policy
  ## @default IfNotPresent
  ## @values Always, IfNotPresent, Never
  pullPolicy: IfNotPresent
  
  ## @param image.tag Container image tag
  ## @default "latest"
  tag: "latest"

## @section Replica configuration

## @param replicaCount Number of replicas to deploy
## @default 2
replicaCount: 2

## @section Service configuration

service:
  ## @param service.type Kubernetes service type
  ## @values ClusterIP, NodePort, LoadBalancer
  type: ClusterIP
  
  ## @param service.port Service port
  port: 80
  
  ## @param service.targetPort Container port
  targetPort: 8080

## @section Resource limits

resources:
  requests:
    ## @param resources.requests.memory Memory request
    memory: "512Mi"
    ## @param resources.requests.cpu CPU request
    cpu: "500m"
  limits:
    ## @param resources.limits.memory Memory limit
    memory: "1Gi"
    ## @param resources.limits.cpu CPU limit
    cpu: "1000m"

## @section Autoscaling configuration

autoscaling:
  ## @param autoscaling.enabled Enable HorizontalPodAutoscaler
  enabled: false
  
  ## @param autoscaling.minReplicas Minimum replicas
  minReplicas: 2
  
  ## @param autoscaling.maxReplicas Maximum replicas
  maxReplicas: 10
  
  ## @param autoscaling.targetCPUUtilizationPercentage Target CPU %
  targetCPUUtilizationPercentage: 80

## @section Security context

securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 3000
  fsGroup: 2000
  seccompProfile:
    type: RuntimeDefault
  readOnlyRootFilesystem: false
  capabilities:
    drop:
    - ALL
```

### Key Features

1. **Complete Coverage**: Every `{{ .Values.* }}` reference from templates has a corresponding value
2. **Inline Documentation**: Uses `## @param` notation for documentation
3. **Type Hints**: Uses `## @values` for enums, `## @default` for defaults
4. **Logical Grouping**: Values grouped into sections (Image, Service, Resources, etc.)
5. **Default Values**: Uses planner output for sensible defaults

### Validation

- Coverage percentage must be >= 95%
- All template variables must be present
- Values match expected types
- Structure is hierarchical and logical

---

## Tool Execution Summary

| Tool | Order | Dependencies | Output File |
|------|-------|--------------|-------------|
| `generate_helpers_tpl` | 1 | None | `templates/_helpers.tpl` |
| `generate_namespace_yaml` | 2 | helpers | `templates/namespace.yaml` |
| `generate_deployment_yaml` | 3 | helpers, namespace? | `templates/deployment.yaml` |
| `generate_service_yaml` | 4 | deployment | `templates/service.yaml` |
| `generate_values_yaml` | N | ALL templates | `values.yaml` |

**Note**: `generate_values_yaml` runs after ALL templates (core + conditional) to ensure complete coverage.

---

**See Also**:
- [Conditional Tools Documentation](./tools-conditional-tools.md)
- [Documentation Tools](./tools-documentation-tools.md)
- [Template Coordinator Documentation](./template-coordinator-documentation.md)
