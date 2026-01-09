# Conditional Template Generation Tools

This document describes the **conditional tools** that are executed based on planner analysis and application requirements.

## Table of Contents

1. [Overview](#overview)
2. [Scaling Tools](#scaling-tools)
3. [Security Tools](#security-tools)
4. [Networking Tools](#networking-tools)
5. [Configuration Tools](#configuration-tools)

---

## Overview

Conditional tools are executed only when specific requirements are detected in the planner output. The coordinator determines which tools to execute based on:

- `planner_output.kubernetes_architecture.resources.auxiliary` - Lists auxiliary resources needed
- `planner_output.application_analysis` - Application characteristics
- `planner_output.parsed_requirements` - User requirements

### Tool Detection Logic

```python
RESOURCE_TO_TOOL = {
    "HorizontalPodAutoscaler": "generate_hpa_yaml",
    "PodDisruptionBudget": "generate_pdb_yaml",
    "NetworkPolicy": "generate_network_policy_yaml",
    "Ingress": "generate_traefik_ingressroute_yaml",
    "ConfigMap": "generate_configmap_yaml",
    "Secret": "generate_secret",
    "ServiceAccount": "generate_service_account_rbac"
}

# Check auxiliary resources
for resource in auxiliary_resources:
    res_type = resource.get("type")
    if res_type in RESOURCE_TO_TOOL:
        conditional_tools.append(RESOURCE_TO_TOOL[res_type])
```

---

## Scaling Tools

### generate_hpa_yaml

**Purpose**: Generate HorizontalPodAutoscaler (HPA) manifest for automatic scaling.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/hpa/hpa_tool.py`

**Condition**: Executed if `HorizontalPodAutoscaler` exists in auxiliary resources

**Dependencies**: `["generate_deployment_yaml"]`

#### Input

Extracts from state:
- `planner_output.scaling_strategy.prod` - Production HPA configuration
- `planner_output.parsed_requirements.deployment.min_replicas`
- `planner_output.parsed_requirements.deployment.max_replicas`
- `planner_output.application_analysis.scalability.target_cpu_utilization`

#### Output Schema

```python
class HPAGenerationToolOutput(BaseModel):
    yaml_content: str
    file_name: str = "hpa.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **API Version**: Uses `autoscaling/v2` (not v1 or v2beta)
2. **Metrics**: Supports CPU, memory, and custom metrics
3. **Scaling Behavior**: Configurable scale-up/down policies (K8s 1.18+)
4. **Target Reference**: References Deployment/StatefulSet by name

#### Example Output

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "myapp.fullname" . }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: {{ .Values.autoscaling.targetMemoryUtilizationPercentage }}
  {{- with .Values.autoscaling.behavior }}
  behavior:
    {{- toYaml . | nindent 4 }}
  {{- end }}
```

#### Critical Requirements

- Target Deployment must exist (dependency)
- Target Deployment must have resource requests defined (for CPU/memory metrics)
- `maxReplicas` > `minReplicas`
- At least one metric required

---

### generate_pdb_yaml

**Purpose**: Generate PodDisruptionBudget (PDB) manifest for high availability.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/pdb/pdb_tool.py`

**Condition**: Executed if `PodDisruptionBudget` exists in auxiliary resources OR `high_availability = true`

**Dependencies**: `["generate_deployment_yaml"]`

#### Input

Extracts from state:
- `planner_output.scaling_strategy.prod.min_available` or `max_unavailable`
- `planner_output.parsed_requirements.deployment.high_availability`
- Deployment selector labels (for pod matching)

#### Output Schema

```python
class PDBGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "pdb.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **API Version**: Uses `policy/v1`
2. **Disruption Budget**: Exactly ONE of `minAvailable` or `maxUnavailable`
3. **Value Types**: Supports integer (e.g., 2) or percentage (e.g., "50%")
4. **Selector Matching**: Must match Deployment pod labels exactly

#### Example Output

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  {{- if .Values.podDisruptionBudget.minAvailable }}
  minAvailable: {{ .Values.podDisruptionBudget.minAvailable }}
  {{- end }}
  {{- if .Values.podDisruptionBudget.maxUnavailable }}
  maxUnavailable: {{ .Values.podDisruptionBudget.maxUnavailable }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "myapp.selectorLabels" . | nindent 6 }}
  {{- if .Values.podDisruptionBudget.unhealthyPodEvictionPolicy }}
  unhealthyPodEvictionPolicy: {{ .Values.podDisruptionBudget.unhealthyPodEvictionPolicy }}
  {{- end }}
```

#### Disruption Budget Strategies

- **2 replicas**: `minAvailable: 1` - Always keeps 1 pod running
- **3+ replicas**: `minAvailable: 2` or `minAvailable: "50%"` - Maintains quorum
- **Stateless apps**: `maxUnavailable: "25%"` - Faster drain operations

---

## Security Tools

### generate_network_policy_yaml

**Purpose**: Generate NetworkPolicy manifest for network segmentation and zero-trust security.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/network_policy/nw_policy_tool.py`

**Condition**: Executed if `NetworkPolicy` exists in auxiliary resources OR `network_policies = true`

**Dependencies**: `["generate_deployment_yaml"]`

#### Input

Extracts from state:
- `planner_output.parsed_requirements.security.network_policies`
- Deployment pod labels (for pod selector)
- Network policy rules from planner

#### Output Schema

```python
class NetworkPolicyGenerationToolOutput(BaseModel):
    yaml_content: str
    file_name: str = "networkpolicy.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **API Version**: Uses `networking.k8s.io/v1`
2. **Policy Types**: Ingress, Egress, or both
3. **Selectors**: Pod selector, namespace selector, IP block
4. **Default Deny**: Empty rules = deny all (be careful!)

#### Example Output

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "myapp.fullname" . }}-netpol
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "myapp.selectorLabels" . | nindent 6 }}
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: frontend
    - namespaceSelector:
        matchLabels:
          name: production
    ports:
    - protocol: TCP
      port: 8080
  egress:
  - to:
    - podSelector:
        matchLabels:
          app: database
    ports:
    - protocol: TCP
      port: 5432
  - to:
    - namespaceSelector:
        matchLabels:
          name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports:
    - protocol: UDP
      port: 53
```

#### Common Patterns

**Deny All Ingress**:
```yaml
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  # No ingress rules = deny all
```

**Allow Same Namespace**:
```yaml
ingress:
- from:
  - podSelector: {}  # All pods in same namespace
```

**Allow DNS Only**:
```yaml
egress:
- to:
  - namespaceSelector:
      matchLabels:
        name: kube-system
    podSelector:
      matchLabels:
        k8s-app: kube-dns
  ports:
  - protocol: UDP
    port: 53
```

#### Critical Requirements

- Pod selector must match Deployment pod labels
- Policy types must be specified (Ingress, Egress, or both)
- Empty rules = deny all (be careful!)
- DNS egress must be allowed for name resolution
- Requires CNI plugin that supports NetworkPolicy (Calico, Cilium, etc.)

---

### generate_service_account_rbac

**Purpose**: Generate ServiceAccount and RBAC (Role/RoleBinding) manifests.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/sa/k8s_sa_tool.py`

**Condition**: Executed if `ServiceAccount` exists in auxiliary resources OR `rbac_required = true`

**Dependencies**: None (can run independently)

#### Input

Extracts from state:
- `planner_output.parsed_requirements.security.rbac_required`
- `planner_output.application_analysis.security.service_account_needed`
- RBAC permissions from planner

#### Output Schema

```python
class ServiceAccountGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "serviceaccount.yaml"  # May include rbac.yaml
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **ServiceAccount**: Custom service account (never use default in production)
2. **RBAC**: Role and RoleBinding if permissions needed
3. **Image Pull Secrets**: Support for private registry authentication
4. **Annotations**: Cloud provider annotations (e.g., AWS IAM role)

#### Example Output

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "myapp.serviceAccountName" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
  {{- with .Values.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- if .Values.serviceAccount.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.serviceAccount.imagePullSecrets }}
- name: {{ . }}
{{- end }}
{{- end }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken }}
```

**With RBAC**:
```yaml
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["get", "list"]

---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ include "myapp.fullname" . }}
subjects:
- kind: ServiceAccount
  name: {{ include "myapp.serviceAccountName" . }}
  namespace: {{ .Release.Namespace }}
```

---

## Networking Tools

### generate_traefik_ingressroute_yaml

**Purpose**: Generate Traefik IngressRoute CRD manifest (Traefik-specific, not standard Ingress).

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/route/traefik_ingressroute_tool.py`

**Condition**: Executed if `Ingress` exists in auxiliary resources AND Traefik is the ingress controller

**Dependencies**: `["generate_service_yaml", "generate_helpers_tpl"]`

**Note**: This tool generates Traefik-specific CRD, not standard Kubernetes Ingress. Standard Ingress is deprecated in favor of Traefik IngressRoute.

**📖 For comprehensive Traefik documentation, see**: [Traefik Comprehensive Guide](../template/traefik-comprehensive-guide.md)

#### Input

Extracts from state:
- `planner_output.parsed_requirements.service.access_type` (if "ingress")
- `planner_output.parsed_requirements.service` (for hostname, TLS)
- Traefik-specific configuration from planner

#### Output Schema

```python
class TraefikIngressRouteToolOutput(BaseModel):
    yaml_content: str
    file_name: str = "ingressroute.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **CRD**: Uses Traefik IngressRoute CRD (`traefik.io/v1alpha1`)
2. **Routes**: Path-based routing with middleware support
3. **TLS**: TLS configuration with certResolver
4. **Middlewares**: Rate limiting, BasicAuth, CORS, StripPrefix

#### Example Output

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  entryPoints:
  - web
  - websecure
  routes:
  - match: Host(`api.example.com`)
    kind: Rule
    services:
    - name: {{ include "myapp.fullname" . }}
      port: {{ .Values.service.port }}
    middlewares:
    {{- if .Values.ingress.middlewares }}
    {{- range .Values.ingress.middlewares }}
    - name: {{ . }}
    {{- end }}
    {{- end }}
  {{- if .Values.ingress.tls }}
  tls:
    certResolver: {{ .Values.ingress.tls.certResolver }}
    {{- if .Values.ingress.tls.domains }}
    domains:
    {{- range .Values.ingress.tls.domains }}
    - main: {{ .main }}
      {{- if .sans }}
      sans:
      {{- range .sans }}
      - {{ . }}
      {{- end }}
      {{- end }}
    {{- end }}
    {{- end }}
  {{- end }}
```

#### Traefik-Specific Features

- **EntryPoints**: `web` (HTTP), `websecure` (HTTPS)
- **CertResolver**: Let's Encrypt integration (e.g., "letsencrypt")
- **Middlewares**: RateLimit, BasicAuth, CORS, StripPrefix, RedirectScheme
- **Route Matching**: Host, Path, Headers

#### Critical Requirements

- Service must exist (dependency)
- Hostname must be specified
- TLS certResolver or secretName required for HTTPS
- Middlewares must be defined separately (not in this tool)

#### 📚 Comprehensive Documentation

**For complete Traefik documentation including:**
- Detailed architecture and concepts
- Matcher syntax reference
- Middleware system guide
- Advanced load balancing (WRR, HRW, Mirroring)
- TLS configuration options
- Migration from standard Ingress
- Real-world examples
- Troubleshooting guide

**See**: [Traefik Comprehensive Guide](./traefik-comprehensive-guide.md)

---

## Configuration Tools

### generate_configmap_yaml

**Purpose**: Generate ConfigMap manifest with application configuration data.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/configmap/config_map_tool.py`

**Condition**: Executed if `ConfigMap` exists in auxiliary resources OR configmaps mentioned in requirements

**Dependencies**: None (can run independently)

#### Input

Extracts from state:
- `planner_output.parsed_requirements.configuration.configmaps_mentioned`
- `planner_output.parsed_requirements.configuration.environment_variables` (non-sensitive)
- ConfigMap data from planner

#### Output Schema

```python
class ConfigMapGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "configmap.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **Data Types**: Key-value pairs, file contents, structured data
2. **Binary Data**: Support for `binaryData` field (base64 encoded)
3. **Immutability**: Optional immutable ConfigMap (K8s 1.21+)
4. **Size Limits**: ConfigMap data must be < 1MB total

#### Example Output

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "myapp.fullname" . }}-config
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
{{- if .Values.configmap.immutable }}
immutable: true
{{- end }}
data:
  # Simple key-value pairs
  DB_HOST: {{ .Values.configmap.dbHost | quote }}
  DB_PORT: {{ .Values.configmap.dbPort | quote }}
  
  # Multi-line configuration files
  config.yaml: |
    server:
      port: {{ .Values.service.targetPort }}
      host: 0.0.0.0
    
  application.properties: |
    spring.datasource.url=jdbc:mysql://{{ .Values.configmap.dbHost }}:{{ .Values.configmap.dbPort }}/mydb
    spring.jpa.hibernate.ddl-auto=update

{{- if .Values.configmap.binaryData }}
binaryData:
  # Binary data (base64 encoded)
  cert.pem: {{ .Values.configmap.binaryData.certPem | b64enc }}
{{- end }}
```

#### Usage Patterns

**As Environment Variables**:
```yaml
# In Deployment:
envFrom:
- configMapRef:
    name: {{ include "myapp.fullname" . }}-config
```

**As Volume Mount**:
```yaml
# In Deployment:
volumes:
- name: config
  configMap:
    name: {{ include "myapp.fullname" . }}-config
    
volumeMounts:
- name: config
  mountPath: /etc/config
  readOnly: true
```

#### Critical Requirements

- Keys must be valid env var names or file names
- Multi-line values use `|` for literal block
- Binary data uses `binaryData` field
- Total size must be < 1MB

---

### generate_secret

**Purpose**: Generate Secret manifest for sensitive data.

**Location**: `k8s_autopilot/core/agents/helm_generator/template/tools/secret/k8s_secret_tool.py`

**Condition**: Executed if `Secret` exists in auxiliary resources OR secrets mentioned in requirements

**Dependencies**: None (can run independently)

#### Input

Extracts from state:
- `planner_output.parsed_requirements.configuration.secrets_mentioned`
- `planner_output.parsed_requirements.configuration.environment_variables` (sensitive vars)
- Secret data from planner

#### Output Schema

```python
class SecretGenerationOutput(BaseModel):
    yaml_content: str
    file_name: str = "secret.yaml"
    template_variables_used: List[str]
    validation_status: Literal["valid", "warning", "error"]
    validation_messages: List[str] = []
    metadata: Dict[str, Any] = {}
```

#### Key Features

1. **Data Encoding**: Values are base64 encoded (Kubernetes requirement)
2. **StringData**: Can use `stringData` field (auto-encoded) for easier templating
3. **Type**: Supports different secret types (Opaque, tls, docker-registry, etc.)
4. **Security**: Never store secrets in values.yaml (use external secret management)

#### Example Output

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "myapp.fullname" . }}-secrets
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
type: Opaque
{{- if .Values.secrets.stringData }}
stringData:
  DB_PASSWORD: {{ .Values.secrets.stringData.dbPassword | quote }}
  API_KEY: {{ .Values.secrets.stringData.apiKey | quote }}
{{- else }}
data:
  DB_PASSWORD: {{ .Values.secrets.data.dbPassword | b64enc }}
  API_KEY: {{ .Values.secrets.data.apiKey | b64enc }}
{{- end }}
```

#### Secret Types

- **Opaque**: Generic secret (default)
- **kubernetes.io/tls**: TLS certificate and key
- **kubernetes.io/dockerconfigjson**: Docker registry credentials
- **kubernetes.io/basic-auth**: Basic authentication

#### Usage Patterns

**As Environment Variables**:
```yaml
# In Deployment:
envFrom:
- secretRef:
    name: {{ include "myapp.fullname" . }}-secrets
```

**As Volume Mount**:
```yaml
# In Deployment:
volumes:
- name: secrets
  secret:
    secretName: {{ include "myapp.fullname" . }}-secrets
    
volumeMounts:
- name: secrets
  mountPath: /etc/secrets
  readOnly: true
```

#### Security Best Practices

- **Never** store secrets in values.yaml
- Use external secret management (Sealed Secrets, External Secrets Operator, Vault)
- Use `stringData` for easier templating (auto-encoded)
- Rotate secrets regularly
- Use RBAC to restrict secret access

---

## Tool Execution Summary

| Tool | Condition | Dependencies | Output File |
|------|-----------|--------------|-------------|
| `generate_hpa_yaml` | HPA in auxiliary resources | deployment | `templates/hpa.yaml` |
| `generate_pdb_yaml` | PDB in auxiliary OR HA=true | deployment | `templates/pdb.yaml` |
| `generate_network_policy_yaml` | NetworkPolicy in auxiliary OR network_policies=true | deployment | `templates/networkpolicy.yaml` |
| `generate_traefik_ingressroute_yaml` | Ingress in auxiliary | service, helpers | `templates/ingressroute.yaml` |
| `generate_configmap_yaml` | ConfigMap in auxiliary OR configmaps mentioned | none | `templates/configmap.yaml` |
| `generate_secret` | Secret in auxiliary OR secrets mentioned | none | `templates/secret.yaml` |
| `generate_service_account_rbac` | ServiceAccount in auxiliary OR rbac_required=true | none | `templates/serviceaccount.yaml` (+ `rbac.yaml`) |

---

## Conditional Tool Detection

The coordinator detects conditional tools by analyzing `planner_output.kubernetes_architecture.resources.auxiliary`:

```python
auxiliary_resources = planner_output.get("kubernetes_architecture", {}).get("resources", {}).get("auxiliary", [])

conditional_tools = []
for resource in auxiliary_resources:
    res_type = resource.get("type")
    if res_type in RESOURCE_TO_TOOL:
        tool_name = RESOURCE_TO_TOOL[res_type]
        if tool_name in TOOL_MAPPING:
            conditional_tools.append(tool_name)
```

**Example**:
- If planner includes `{"type": "HorizontalPodAutoscaler"}` → `generate_hpa_yaml` added
- If planner includes `{"type": "Ingress"}` → `generate_traefik_ingressroute_yaml` added
- If `high_availability = true` → `generate_pdb_yaml` added (even if not in auxiliary)

---

**See Also**:
- [Core Tools Documentation](./tools-core-tools.md)
- [Documentation Tools](./tools-documentation-tools.md)
- [Template Coordinator Documentation](./template-coordinator-documentation.md)
