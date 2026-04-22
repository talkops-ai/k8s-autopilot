"""
Skill Templates Factory
-----------------------
Production-grade template rendering engine that transforms the full
helm_planner pipeline output into agentskills.io compliant skill directories.

Consumes data from ALL upstream logic:
  - parsed_requirements   (from req_analyser_tool → ParsedRequirements)
  - application_analysis  (from arch_planner_tool → ApplicationAnalysisOutput)
  - kubernetes_architecture (from arch_planner_tool → KubernetesArchitectureOutput)
  - resource_estimation   (from arch_planner_tool → ResourceEstimationOutput)
  - scaling_strategy      (from arch_planner_tool → ScalingStrategyOutput)
  - dependencies          (from arch_planner_tool → DependenciesOutput)

Design Principles:
  - Per-app skills provide DATA-LEVEL references (what to configure)
  - The generic helm-generator skill provides TEMPLATE-LEVEL patterns (how to template)
  - Per-app SKILL.md tells the generator to cross-reference both
  - Output quality targets Bitnami Helm chart standards
"""
import json
import re
from typing import Dict, Any, List, Optional


def _slugify(name: str) -> str:
    """Convert a name to a lowercase-hyphenated slug safe for paths."""
    slug = name.lower().strip()
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug


# ---------------------------------------------------------------------------
# Helper: safe nested dict access
# ---------------------------------------------------------------------------

def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Safe nested dict access: _get(d, 'a', 'b', 'c') -> d['a']['b']['c']."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d if d is not None else default


# ---------------------------------------------------------------------------
# Helper: format resource spec as YAML string
# ---------------------------------------------------------------------------

def _resource_spec_yaml(spec: Dict[str, Any], indent: int = 2) -> str:
    """Format an EnvironmentResourceSpec as clean YAML."""
    prefix = " " * indent
    lines = []
    requests = spec.get("requests", {})
    limits = spec.get("limits", {})
    if requests:
        lines.append(f"{prefix}requests:")
        lines.append(f"{prefix}  cpu: {requests.get('cpu', '100m')}")
        lines.append(f"{prefix}  memory: {requests.get('memory', '128Mi')}")
    if limits:
        lines.append(f"{prefix}limits:")
        lines.append(f"{prefix}  cpu: {limits.get('cpu', '500m')}")
        lines.append(f"{prefix}  memory: {limits.get('memory', '512Mi')}")
    return "\n".join(lines)


# ===========================================================================
# 1. SKILL.md — Main progressive-disclosure document
# ===========================================================================

def render_skill_md(
    app_name: str,
    parsed_requirements: Dict[str, Any],
    application_analysis: Dict[str, Any],
    kubernetes_architecture: Dict[str, Any],
    resource_estimation: Optional[Dict[str, Any]] = None,
    scaling_strategy: Optional[Dict[str, Any]] = None,
    dependencies: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the main SKILL.md following agentskills.io specification v1.

    Follows the Agent Skills specification and best practices:
    - Description: imperative phrasing, keyword-rich, ≤1024 chars
    - Body: <5000 tokens, procedural steps (not declarative tables)
    - Progressive disclosure: heavy data lives in references/
    - Safety Rules, Gotchas, Validation loops, Response Format
    - Matches quality of argo-rollouts-gitops skill
    """
    app_slug = _slugify(app_name)

    # -- Extract from parsed_requirements --
    app_type = parsed_requirements.get("app_type", "web-application")
    language = parsed_requirements.get("language", "unknown")
    namespace = parsed_requirements.get("namespace") or "default"
    image_info = parsed_requirements.get("image", {})
    image_repo = image_info.get("repository", "")
    image_tag = image_info.get("tag", "latest")
    full_image = image_info.get("full_image", f"{image_repo}:{image_tag}")

    deployment_info = parsed_requirements.get("deployment", {})
    min_replicas = deployment_info.get("min_replicas", 1)
    max_replicas = deployment_info.get("max_replicas", 3)

    # -- Extract from application_analysis --
    framework = application_analysis.get("framework_analysis", {})
    scalability = application_analysis.get("scalability", {})
    networking = application_analysis.get("networking", {})
    security = application_analysis.get("security", {})

    stateless = scalability.get("stateless", True)
    state_label = "stateless" if stateless else "stateful"
    hpa_enabled = scalability.get("hpa_enabled", False)
    container_port = networking.get("port", 8080)
    protocol = networking.get("protocol", "http")
    tls_needed = networking.get("tls_needed", False)

    # -- Extract from kubernetes_architecture --
    k8s_res = kubernetes_architecture.get("resources", {})
    core_resources = k8s_res.get("core", [])
    aux_resources = k8s_res.get("auxiliary", [])
    arch_pattern = k8s_res.get("architecture_pattern", "stateless_microservice")
    estimated_complexity = k8s_res.get("estimated_complexity", "medium")

    workload_type = "Deployment"
    for cr in core_resources:
        if cr.get("type") in ("Deployment", "StatefulSet", "DaemonSet"):
            workload_type = cr.get("type")
            break

    all_resource_types = [cr.get("type", "") for cr in core_resources]
    all_resource_types += [ar.get("type", "") for ar in aux_resources]
    all_resource_types = [t for t in all_resource_types if t]

    # -- Extract from dependencies --
    helm_deps = []
    init_containers = []
    sidecars = []
    helm_hooks = []
    if dependencies:
        helm_deps = dependencies.get("helm_dependencies", [])
        init_containers = dependencies.get("init_containers_needed", [])
        sidecars = dependencies.get("sidecars_needed", [])
        helm_hooks = dependencies.get("helm_hooks", [])

    # -- Ingress details --
    ingress_res = next(
        (r for r in aux_resources if r.get("type") == "Ingress"), None
    )
    ingress_host = ""
    if ingress_res:
        hints = ingress_res.get("configuration_hints", {})
        ingress_host = hints.get("hostname", "")

    # -- Namespace --
    ns_core = next(
        (cr for cr in core_resources if cr.get("type") == "Namespace"), None
    )
    ns_name = namespace
    if ns_core:
        ns_name = _get(
            ns_core, "key_configuration_parameters", "name",
            default=namespace,
        )

    # -- Template files to generate --
    template_files = _compute_template_file_list(
        workload_type, aux_resources, helm_deps,
        init_containers, sidecars, helm_hooks,
    )

    # -- Build auxiliary resource list for keywords --
    aux_keywords = []
    if "Ingress" in all_resource_types:
        aux_keywords.append("Ingress")
    if hpa_enabled:
        aux_keywords.append("HPA")
    if "NetworkPolicy" in all_resource_types:
        aux_keywords.append("NetworkPolicy")
    if "PodDisruptionBudget" in all_resource_types:
        aux_keywords.append("PDB")
    dep_names = [d.get("name", "") for d in helm_deps]
    ic_names = [ic.get("name", "") for ic in init_containers]
    sc_names = [sc.get("name", "") for sc in sidecars]

    # -- Build description (imperative, ≤1024 chars, keyword-rich) --
    # Following agentskills.io: "Use imperative phrasing. Focus on user intent."
    desc = (
        f"Generates Bitnami-standard Helm charts for {app_name} "
        f"({app_type}, {language}). Use when the user asks to create, "
        f"scaffold, deploy, or configure {app_name} on Kubernetes, "
        f"or when generating Helm chart templates for {app_type} workloads"
    )
    if ingress_host:
        desc += f" with ingress at {ingress_host}"
    desc += (
        ". Also use when the user mentions Helm chart, values.yaml, "
        f"{workload_type.lower()}, or Kubernetes manifest for {app_name} "
        "— even if they don't say 'Helm' explicitly. "
    )
    # Add trigger keywords
    kw_parts = [
        app_name, app_slug, "Helm chart", "values.yaml",
        workload_type, app_type, language, "Kubernetes deploy",
    ]
    kw_parts += aux_keywords + dep_names + ic_names
    kw_parts = [k for k in kw_parts if k]
    desc += "Triggers on keywords: " + ", ".join(kw_parts) + "."
    # Enforce 1024 char limit
    if len(desc) > 1024:
        desc = desc[:1020] + "..."

    # -- Probes --
    liveness_path = framework.get("liveness_probe_path", "/healthz")
    readiness_path = framework.get("readiness_probe_path", "/readyz")
    initial_delay = framework.get("initial_delay_seconds", 15)
    shutdown_period = framework.get("graceful_shutdown_period", 30)

    # ==== Begin SKILL.md rendering ====

    template = f"""---
name: {app_slug}-chart-generator
description: >-
  {desc}
compatibility: >-
  Requires write_file, read_file, edit_file, ls, grep tools.
  Virtual filesystem rooted at /workspace/. The helm-generator generic skill
  must be available at /skills/helm-operator/helm-generator/ for Go template
  patterns.
metadata:
  author: helm-planner
  version: "1.0"
  generated-by: planner-skill-writer
  architecture-pattern: {arch_pattern}
  complexity: {estimated_complexity}
allowed-tools: write_file read_file edit_file ls grep
---

# {app_name} Helm Chart Generator

## When to Use

Use this skill for any Helm chart generation request involving {app_name}:
creating a new chart from scratch, scaffolding templates, writing values.yaml,
configuring {workload_type} manifests, or setting up {', '.join(aux_keywords) if aux_keywords else 'auxiliary resources'}.
This skill provides the **data-level** architecture specifications. Read
the generic `helm-generator` skill for **template-level** Go template patterns.

## Reference Files (Progressive Disclosure)

Read `references/execution-blueprint.md` **first** — it contains all K8s
resource specifications. Load other references only as needed per step.

| Reference | Contents | Read during |
|---|---|---|
| `execution-blueprint.md` | All K8s resources, configs, tradeoffs, justifications | Step 1 (always) |
| `values-schema.md` | Complete `values.yaml` structure with defaults | Step 3 |
| `scaling-and-resources.md` | Per-env HPA, PDB, scaling behavior, monitoring | Step 4 |
| `security-blueprint.md` | SecurityContext, NetworkPolicy, RBAC, SA config | Step 4 |
| `manifest-patterns.md` | Expected rendered YAML per resource | Cross-reference |

Also read generic patterns from `/skills/helm-operator/helm-generator/references/`:
`helpers-and-values.md`, `deployment-pattern.md`, `service-pattern.md`,
`autoscaling-pattern.md`, `ingress-pattern.md`.

## Chart Generation Workflow

Progress:
- [ ] Step 1: Read `execution-blueprint.md`
- [ ] Step 2: Write `Chart.yaml` with metadata
- [ ] Step 3: Read `values-schema.md`, write `values.yaml`
- [ ] Step 4: Read `security-blueprint.md` and `scaling-and-resources.md`
- [ ] Step 5: Write `templates/_helpers.tpl` (use `helpers-and-values.md` pattern)
- [ ] Step 6: Write core templates ({workload_type.lower()}, service)
- [ ] Step 7: Write auxiliary templates (ingress, hpa, pdb, networkpolicy, etc.)
- [ ] Step 8: Write NOTES.txt
- [ ] Step 9: Validate and return summary

### Step 1. Load Architecture Data

Read `references/execution-blueprint.md` to understand the complete resource
topology. This chart produces the following templates:

```
{app_slug}/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── _helpers.tpl
"""
    for tf in template_files:
        template += f"│   ├── {tf}\n"
    template += f"""│   └── NOTES.txt
└── README.md
```

### Step 2. Write Chart.yaml
"""

    template += f"""Write `Chart.yaml` with chart metadata (name, version, appVersion).

### Step 3. Write values.yaml

Read `references/values-schema.md` — it contains the **complete, production-ready
`values.yaml` structure** with all sections and defaults. Copy that structure
directly, adjusting only if needed.

### Step 4. Write Templates

For each template file, follow this procedure:

1. Read the relevant **data reference** from this skill (e.g., `security-blueprint.md` for `serviceaccount.yaml`)
2. Read the corresponding **Go template pattern** from the generic `helm-generator` skill
3. Write the template, merging the pattern with this app's specific configuration
4. Verify every `Values.*` reference has a matching key in `values.yaml`

### Step 5. Validate

Run validation checks on every generated file:

1. Verify all templates reference `Values` keys that exist in `values.yaml`
2. Verify no hardcoded namespace — always use `{{{{ .Release.Namespace }}}}`
3. Verify all optional resources are guarded by `.Values.<resource>.enabled`
4. Verify `_helpers.tpl` defines: `{app_slug}.name`, `{app_slug}.fullname`, `{app_slug}.labels`, `{app_slug}.selectorLabels`, `{app_slug}.chart`, `{app_slug}.serviceAccountName`

If validation fails, fix the issues and re-validate before returning.

## Safety Rules — MUST Follow

1. **Never hardcode namespaces.** All resources MUST use `{{{{ .Release.Namespace }}}}` or `{{{{ .Values.namespace }}}}`. Exception: the `namespace.yaml` template itself may set a name.

2. **All optional resources require `.enabled` toggles.** Ingress, HPA, PDB, NetworkPolicy, ServiceAccount — wrap each template in `{{{{- if .Values.<resource>.enabled }}}}`. Do not assume any optional resource is always deployed.

3. **Image tag must default to `.Chart.AppVersion`.** Use `{{{{ .Values.image.tag | default .Chart.AppVersion }}}}` — never hardcode an image tag in templates.

4. **Resource limits are mandatory.** Every container (including init containers and sidecars) MUST have `resources.requests` and `resources.limits`. Containers without limits allow unbounded resource consumption and prevent HPA from functioning.

5. **Security context is mandatory.** Every pod MUST include `securityContext` with `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, and `capabilities.drop: [ALL]` per Bitnami convention.

6. **Liveness and readiness probes are mandatory.** Every long-running container MUST have both probes. Use the paths from `execution-blueprint.md`. Include a startup probe for containers with slow initialization (`initialDelaySeconds > 10`).

7. **Do not mix `minAvailable` and `maxUnavailable` in PDB.** Use exactly one. Prefer `maxUnavailable: 1` for most workloads.

8. **Subchart values must be namespaced.** Override subchart values under their chart name key (e.g., `postgresql.auth.database`). Never use global values that collide with subchart internal keys.

## Gotchas

- The `_helpers.tpl` name prefix MUST match the chart name exactly. If the chart is `{app_slug}`, every helper MUST start with `{app_slug}.` (e.g., `{app_slug}.fullname`). Mismatched prefixes cause silent template failures.

- When `autoscaling.enabled` is `true`, the {workload_type} template **MUST NOT** set `.spec.replicas`. Otherwise HPA and the static replica count fight, causing constant scaling churn.

- Init containers share the pod's `serviceAccountName` and `securityContext`. If you drop capabilities on the main container, the same restrictions apply to init containers. Ensure init container images work under those restrictions.
"""

    if tls_needed:
        template += (
            "- TLS is required. The Ingress template must include a `tls` block referencing "
            "a Secret name from `.Values.ingress.tls`. Do not generate self-signed certs in templates.\n\n"
        )

    template += f"""- `NOTES.txt` must use `{{{{- include "{app_slug}.fullname" . }}}}` for the release name, not `{{{{ .Release.Name }}}}` directly, to stay consistent with how resources are named.

## Response Format

- For chart generation, return a **manifest summary** listing every file written with a one-line description of what it contains.
- For each template file, note which `Values.*` keys it consumes.
- If any design decision deviates from the reference blueprints (e.g., omitting an optional resource), explain why.
- End with: "Generated N files for {app_name} Helm Chart at ./workspace/helm-charts/{app_slug}/."
"""
    return template


def _compute_template_file_list(
    workload_type: str,
    aux_resources: List[Dict],
    helm_deps: List[Dict],
    init_containers: List[Dict],
    sidecars: List[Dict],
    helm_hooks: List[Dict],
) -> List[str]:
    """Compute the list of template files the generator should create."""
    files = []

    # Primary workload
    wtype = workload_type.lower()
    if wtype == "statefulset":
        files.append("statefulset.yaml")
    elif wtype == "daemonset":
        files.append("daemonset.yaml")
    else:
        files.append("deployment.yaml")

    # Map auxiliary resource types to template filenames
    aux_type_map = {
        "Service": "service.yaml",
        "Ingress": "ingress.yaml",
        "HorizontalPodAutoscaler": "hpa.yaml",
        "PodDisruptionBudget": "pdb.yaml",
        "ConfigMap": "configmap.yaml",
        "Secret": "secret.yaml",
        "ServiceAccount": "serviceaccount.yaml",
        "NetworkPolicy": "networkpolicy.yaml",
        "PersistentVolumeClaim": "pvc.yaml",
        "ResourceQuota": "resourcequota.yaml",
        "LimitRange": "limitrange.yaml",
        "VerticalPodAutoscaler": "vpa.yaml",
    }
    for ar in aux_resources:
        rtype = ar.get("type", "")
        fname = aux_type_map.get(rtype)
        if fname and fname not in files:
            files.append(fname)

    # Helm hooks
    for hook in helm_hooks:
        hook_name = hook.get("name", "hook")
        fname = f"hooks/{hook_name}-job.yaml"
        if fname not in files:
            files.append(fname)

    return files


# ===========================================================================
# 2. Execution Blueprint — complete K8s resource architecture
# ===========================================================================

def render_execution_blueprint(
    app_name: str,
    architecture: Dict[str, Any],
) -> str:
    """Render the execution blueprint with FULL resource specifications.

    This is the primary data reference the helm-generator uses to understand
    what Kubernetes resources to create and how to configure them.
    """
    k8s_res = architecture.get("resources", {})
    core_resources = k8s_res.get("core", [])
    aux_resources = k8s_res.get("auxiliary", [])
    arch_pattern = k8s_res.get("architecture_pattern", "N/A")
    complexity = k8s_res.get("estimated_complexity", "N/A")

    bp = f"# {app_name.upper()} — Execution Blueprint\n\n"
    bp += f"**Architecture Pattern**: `{arch_pattern}`\n"
    bp += f"**Estimated Complexity**: `{complexity}`\n\n"
    bp += "---\n\n"

    # ── Core Resources ──
    bp += "## Core Resources\n\n"
    if not core_resources:
        bp += "> No core resources defined.\n\n"
    for cr in core_resources:
        rtype = cr.get("type", "Unknown")
        bp += f"### {rtype}\n\n"

        # Key configuration parameters
        kcp = cr.get("key_configuration_parameters", {})
        if kcp:
            bp += "**Key Configuration Parameters:**\n\n"
            bp += "| Parameter | Value |\n|---|---|\n"
            for key, val in kcp.items():
                if isinstance(val, dict):
                    val_str = ", ".join(f"`{k}: {v}`" for k, v in val.items())
                elif isinstance(val, list):
                    val_str = ", ".join(f"`{v}`" for v in val)
                else:
                    val_str = f"`{val}`"
                bp += f"| {key} | {val_str} |\n"
            bp += "\n"

        # Alternatives considered
        alts = cr.get("alternatives_considered", [])
        if alts:
            bp += "**Alternatives Considered:**\n\n"
            for alt in alts:
                bp += f"- {alt}\n"
            bp += "\n"

    # ── Auxiliary Resources ──
    bp += "---\n\n## Auxiliary Resources\n\n"
    if not aux_resources:
        bp += "> No auxiliary resources defined.\n\n"

    for ar in aux_resources:
        rtype = ar.get("type", "Unknown")
        criticality = ar.get("criticality", "optional")
        env_specific = ar.get("environment_specific")
        deps = ar.get("dependencies", [])
        tradeoffs = ar.get("tradeoffs", "")
        hints = ar.get("configuration_hints", {})

        bp += f"### {rtype}\n\n"
        bp += f"- **Criticality**: `{criticality}`\n"
        if env_specific:
            bp += f"- **Environment**: `{env_specific}` only\n"
        if deps:
            bp += f"- **Depends On**: {', '.join(f'`{d}`' for d in deps)}\n"
        bp += "\n"

        # Configuration hints
        if hints:
            bp += "**Configuration:**\n\n"
            bp += "| Parameter | Value |\n|---|---|\n"
            for key, val in hints.items():
                if key == "justification":
                    continue  # shown separately
                if isinstance(val, dict):
                    val_str = ", ".join(f"`{k}: {v}`" for k, v in val.items())
                elif isinstance(val, list):
                    val_str = ", ".join(f"`{v}`" for v in val)
                elif isinstance(val, bool):
                    val_str = "Yes" if val else "No"
                else:
                    val_str = f"`{val}`"
                bp += f"| {key} | {val_str} |\n"
            bp += "\n"

            # Justification
            justification = hints.get("justification", "")
            if justification:
                bp += f"> **Rationale**: {justification}\n\n"

        # Tradeoffs
        if tradeoffs:
            bp += f"⚠️ **Tradeoffs**: {tradeoffs}\n\n"

    return bp


# ===========================================================================
# 3. Values Schema — complete values.yaml structure
# ===========================================================================

def render_values_schema(
    parsed_reqs: Dict[str, Any],
    resources: Optional[Dict[str, Any]],
    scaling: Optional[Dict[str, Any]],
    application_analysis: Optional[Dict[str, Any]] = None,
    kubernetes_architecture: Optional[Dict[str, Any]] = None,
    dependencies: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a complete, production-grade values.yaml schema.

    This is the definitive reference for what the generated values.yaml
    should contain, with actual defaults derived from the planner output.
    """
    # -- Image --
    image_info = parsed_reqs.get("image", {})
    image_repo = image_info.get("repository", "nginx")
    image_tag = image_info.get("tag", "latest")

    # -- Deployment --
    deployment = parsed_reqs.get("deployment", {})
    min_replicas = deployment.get("min_replicas", 1)
    max_replicas = deployment.get("max_replicas", 3)

    # -- Networking from analysis --
    networking = {}
    if application_analysis:
        networking = application_analysis.get("networking", {})
    container_port = networking.get("port", 8080)
    protocol = networking.get("protocol", "http")
    tls_needed = networking.get("tls_needed", False)

    # -- Security from analysis --
    security = {}
    if application_analysis:
        security = application_analysis.get("security", {})

    # -- Framework from analysis --
    framework = {}
    if application_analysis:
        framework = application_analysis.get("framework_analysis", {})

    # -- Ingress details from architecture --
    ingress_hints = {}
    if kubernetes_architecture:
        aux = _get(kubernetes_architecture, "resources", "auxiliary", default=[])
        for r in aux:
            if r.get("type") == "Ingress":
                ingress_hints = r.get("configuration_hints", {})
                break

    # -- Service details from architecture --
    service_hints = {}
    if kubernetes_architecture:
        aux = _get(kubernetes_architecture, "resources", "auxiliary", default=[])
        for r in aux:
            if r.get("type") == "Service":
                service_hints = r.get("configuration_hints", {})
                break

    svc_type = service_hints.get("type", "ClusterIP")
    svc_port = service_hints.get("port", 80)
    svc_target_port = service_hints.get("target_port", container_port)

    # -- HPA config from scaling --
    hpa_enabled = False
    if scaling:
        prod_scaling = scaling.get("prod", {})
        if prod_scaling.get("min_replicas") and prod_scaling.get("max_replicas"):
            hpa_enabled = True

    out = "# Recommended `values.yaml` Schema\n\n"
    out += "This is the complete values structure the Helm chart should implement.\n"
    out += "Every parameter listed here MUST have a default value in `values.yaml`.\n\n"

    out += "```yaml\n"
    out += "# =============================================================================\n"
    out += "# CORE CONFIGURATION\n"
    out += "# =============================================================================\n\n"

    out += f"replicaCount: {min_replicas}\n\n"

    out += "image:\n"
    out += f"  repository: {image_repo}\n"
    out += "  pullPolicy: IfNotPresent\n"
    out += f'  tag: "{image_tag}"\n\n'

    out += "imagePullSecrets: []\n"
    out += 'nameOverride: ""\n'
    out += 'fullnameOverride: ""\n\n'

    # Service Account
    sa_needed = security.get("service_account_needed", True)
    out += "# =============================================================================\n"
    out += "# SERVICE ACCOUNT\n"
    out += "# =============================================================================\n\n"
    out += "serviceAccount:\n"
    out += f"  create: {str(sa_needed).lower()}\n"
    out += "  automountServiceAccountToken: false\n"
    out += "  annotations: {}\n"
    out += '  name: ""\n\n'

    # Pod Security
    out += "# =============================================================================\n"
    out += "# SECURITY CONTEXT\n"
    out += "# =============================================================================\n\n"
    out += "podSecurityContext:\n"
    out += "  fsGroup: 1000\n\n"
    out += "securityContext:\n"
    out += f"  runAsNonRoot: {str(security.get('run_as_non_root', True)).lower()}\n"
    out += "  runAsUser: 1000\n"
    out += "  runAsGroup: 1000\n"
    out += f"  readOnlyRootFilesystem: {str(security.get('read_only_root_filesystem', False)).lower()}\n"
    out += "  allowPrivilegeEscalation: false\n"
    out += "  capabilities:\n"
    out += "    drop:\n"
    caps = security.get("capabilities_to_drop", ["ALL"])
    for cap in caps:
        out += f"      - {cap}\n"
    out += "\n"

    # Service
    out += "# =============================================================================\n"
    out += "# SERVICE\n"
    out += "# =============================================================================\n\n"
    out += "service:\n"
    out += f"  type: {svc_type}\n"
    out += f"  port: {svc_port}\n"
    out += f"  targetPort: {svc_target_port}\n"
    out += f"  protocol: {protocol.upper()}\n\n"

    # Ingress
    ingress_host = ingress_hints.get("hostname", "chart-example.local")
    ingress_tls = ingress_hints.get("tls_enabled", tls_needed)
    ingress_annotations = ingress_hints.get("annotations", {})

    out += "# =============================================================================\n"
    out += "# INGRESS\n"
    out += "# =============================================================================\n\n"
    out += "ingress:\n"
    out += f"  enabled: {'true' if ingress_hints else 'false'}\n"
    out += "  entryPoints:\n"
    out += "    - web\n"
    if ingress_tls:
        out += "    - websecure\n"
    out += "  annotations:\n"
    if ingress_annotations:
        for k, v in ingress_annotations.items():
            out += f"    {k}: \"{v}\"\n"
    else:
        out += "    {}\n"
    out += "  hosts:\n"
    out += f"    - host: {ingress_host}\n"
    out += "      path: /\n"
    out += "  middlewares: []\n"
    out += "  sticky: false\n"
    if ingress_tls:
        out += "  tls:\n"
        out += f"    secretName: {_slugify(ingress_host)}-tls\n"
    else:
        out += "  tls: {}\n"
    out += "\n"

    # Health Probes
    liveness_path = framework.get("liveness_probe_path", "/healthz")
    readiness_path = framework.get("readiness_probe_path", "/readyz")
    initial_delay = framework.get("initial_delay_seconds", 15)
    shutdown = framework.get("graceful_shutdown_period", 30)

    out += "# =============================================================================\n"
    out += "# HEALTH PROBES\n"
    out += "# =============================================================================\n\n"
    out += "livenessProbe:\n"
    out += "  httpGet:\n"
    out += f"    path: {liveness_path}\n"
    out += f"    port: {container_port}\n"
    out += f"  initialDelaySeconds: {initial_delay}\n"
    out += "  periodSeconds: 10\n"
    out += "  timeoutSeconds: 5\n"
    out += "  failureThreshold: 3\n\n"

    out += "readinessProbe:\n"
    out += "  httpGet:\n"
    out += f"    path: {readiness_path}\n"
    out += f"    port: {container_port}\n"
    out += f"  initialDelaySeconds: {max(5, initial_delay - 10)}\n"
    out += "  periodSeconds: 5\n"
    out += "  timeoutSeconds: 3\n"
    out += "  failureThreshold: 3\n\n"

    out += "startupProbe:\n"
    out += "  httpGet:\n"
    out += f"    path: {readiness_path}\n"
    out += f"    port: {container_port}\n"
    out += f"  initialDelaySeconds: 0\n"
    out += f"  periodSeconds: 5\n"
    out += f"  failureThreshold: {max(12, (initial_delay * 2) // 5)}\n\n"

    out += f"terminationGracePeriodSeconds: {shutdown}\n\n"

    # Resources — per environment
    out += "# =============================================================================\n"
    out += "# RESOURCES (production defaults)\n"
    out += "# =============================================================================\n\n"
    out += "resources:\n"
    if resources:
        prod = resources.get("prod", {})
        prod_req = prod.get("requests", {})
        prod_lim = prod.get("limits", {})
        out += "  requests:\n"
        out += f"    cpu: {prod_req.get('cpu', '250m')}\n"
        out += f"    memory: {prod_req.get('memory', '256Mi')}\n"
        out += "  limits:\n"
        out += f"    cpu: {prod_lim.get('cpu', '500m')}\n"
        out += f"    memory: {prod_lim.get('memory', '512Mi')}\n"
    else:
        out += "  requests:\n"
        out += "    cpu: 100m\n"
        out += "    memory: 128Mi\n"
        out += "  limits:\n"
        out += "    cpu: 500m\n"
        out += "    memory: 512Mi\n"
    out += "\n"

    # Autoscaling
    out += "# =============================================================================\n"
    out += "# AUTOSCALING\n"
    out += "# =============================================================================\n\n"
    out += "autoscaling:\n"
    out += f"  enabled: {str(hpa_enabled).lower()}\n"
    if scaling and hpa_enabled:
        prod_sc = scaling.get("prod", {})
        out += f"  minReplicas: {prod_sc.get('min_replicas', min_replicas)}\n"
        out += f"  maxReplicas: {prod_sc.get('max_replicas', max_replicas)}\n"
        out += f"  targetCPUUtilizationPercentage: {prod_sc.get('target_cpu_utilization', 70)}\n"
        target_mem = prod_sc.get("target_memory_utilization")
        if target_mem:
            out += f"  targetMemoryUtilizationPercentage: {target_mem}\n"
    else:
        out += f"  minReplicas: {min_replicas}\n"
        out += f"  maxReplicas: {max_replicas}\n"
        out += "  targetCPUUtilizationPercentage: 80\n"
    out += "\n"

    # PDB
    out += "# =============================================================================\n"
    out += "# POD DISRUPTION BUDGET\n"
    out += "# =============================================================================\n\n"
    out += "pdb:\n"
    pdb_enabled = False
    if scaling:
        prod_sc = scaling.get("prod", {})
        if prod_sc.get("max_unavailable") is not None or prod_sc.get("min_available") is not None:
            pdb_enabled = True
    out += f"  enabled: {str(pdb_enabled).lower()}\n"
    if pdb_enabled and scaling:
        prod_sc = scaling.get("prod", {})
        mu = prod_sc.get("max_unavailable")
        ma = prod_sc.get("min_available")
        if mu is not None:
            out += f"  maxUnavailable: {mu}\n"
        if ma is not None:
            out += f"  minAvailable: {ma}\n"
    out += "\n"

    # NetworkPolicy
    out += "# =============================================================================\n"
    out += "# NETWORK POLICY\n"
    out += "# =============================================================================\n\n"
    out += "networkPolicy:\n"
    out += "  enabled: false\n\n"

    # Node scheduling
    out += "# =============================================================================\n"
    out += "# NODE SCHEDULING\n"
    out += "# =============================================================================\n\n"
    out += "nodeSelector: {}\n"
    out += "tolerations: []\n"
    out += "affinity: {}\n"
    out += "podAnnotations: {}\n"
    out += "podLabels: {}\n\n"

    # ConfigMap / Secrets
    config_analysis = {}
    if application_analysis:
        config_analysis = application_analysis.get("configuration", {})
    out += "# =============================================================================\n"
    out += "# APPLICATION CONFIGURATION\n"
    out += "# =============================================================================\n\n"
    out += "config:\n"
    out += "  # Environment variables to inject via ConfigMap\n"
    out += "  env: {}\n"
    out += "  # Example:\n"
    out += "  #   LOG_LEVEL: info\n"
    out += "  #   APP_PORT: \"8080\"\n\n"
    out += "secrets:\n"
    out += "  # Sensitive values to inject via Secret\n"
    out += "  data: {}\n"
    out += "  # Example:\n"
    out += "  #   DATABASE_URL: \"postgresql://...\"\n"
    out += "  #   API_KEY: \"...\"\n\n"

    # Dependency chart overrides
    if dependencies:
        helm_deps = dependencies.get("helm_dependencies", [])
        if helm_deps:
            out += "# =============================================================================\n"
            out += "# SUBCHART OVERRIDES\n"
            out += "# =============================================================================\n\n"
            for dep in helm_deps:
                dep_name = dep.get("name", "unknown")
                condition = dep.get("condition", f"{dep_name}.enabled")
                out += f"# {dep.get('reason', '')}\n"
                out += f"{dep_name}:\n"
                out += f"  enabled: false  # Set to true to deploy {dep_name} subchart\n"
                out += f"  # Override {dep_name} values here\n"
                out += "  # See https://artifacthub.io for available values\n\n"

    out += "```\n\n"

    # Per-environment profiles table
    if resources:
        out += "## Per-Environment Resource Profiles\n\n"
        out += "Use these profiles for environment-specific overrides:\n\n"
        out += "| Environment | CPU Req | CPU Lim | Mem Req | Mem Lim | QoS | Headroom |\n"
        out += "|---|---|---|---|---|---|---|\n"
        for env_key, env_label in [("dev", "Development"), ("staging", "Staging"), ("prod", "Production")]:
            env_spec = resources.get(env_key, {})
            req = env_spec.get("requests", {})
            lim = env_spec.get("limits", {})
            qos = env_spec.get("qos_class", "Burstable")
            headroom = env_spec.get("scaling_headroom_percent", "N/A")
            out += (
                f"| {env_label} | {req.get('cpu', 'N/A')} | {lim.get('cpu', 'N/A')} "
                f"| {req.get('memory', 'N/A')} | {lim.get('memory', 'N/A')} "
                f"| {qos} | {headroom}% |\n"
            )
        out += "\n"

        # Reasoning
        reasoning = resources.get("reasoning", "")
        if reasoning:
            out += f"> **Estimation Rationale**: {reasoning}\n\n"

        # Cost notes
        cost_notes = resources.get("cost_optimization_notes", "")
        if cost_notes:
            out += f"> **Cost Optimization**: {cost_notes}\n\n"

    return out


# ===========================================================================
# 4. Scaling and Resources — HPA, PDB, behavior, monitoring
# ===========================================================================

def render_scaling_and_resources(
    scaling: Optional[Dict[str, Any]],
    resources: Optional[Dict[str, Any]],
) -> str:
    """Render per-environment scaling and resource allocation."""
    out = "# Scaling and Resource Allocation\n\n"
    out += "This reference defines the complete HPA, PDB, and scaling behavior\n"
    out += "configuration across all environments.\n\n"

    if not scaling and not resources:
        out += "> No scaling or resource data available.\n"
        return out

    # -- Per-environment HPA configuration --
    if scaling:
        out += "## HPA Configuration by Environment\n\n"
        out += "| Property | Development | Staging | Production |\n"
        out += "|---|---|---|---|\n"

        env_keys = ["dev", "staging", "prod"]
        env_data = {k: scaling.get(k, {}) for k in env_keys}

        props = [
            ("Min Replicas", "min_replicas"),
            ("Max Replicas", "max_replicas"),
            ("Target CPU %", "target_cpu_utilization"),
            ("Target Memory %", "target_memory_utilization"),
        ]
        for label, key in props:
            vals = [str(env_data[e].get(key, "N/A")) for e in env_keys]
            out += f"| {label} | {' | '.join(vals)} |\n"
        out += "\n"

        # PDB per environment
        out += "## PDB Configuration by Environment\n\n"
        out += "| Property | Development | Staging | Production |\n"
        out += "|---|---|---|---|\n"
        pdb_props = [
            ("Max Unavailable", "max_unavailable"),
            ("Min Available", "min_available"),
            ("Eviction Policy", "unhealthy_pod_eviction_policy"),
        ]
        for label, key in pdb_props:
            vals = [str(env_data[e].get(key, "N/A")) for e in env_keys]
            out += f"| {label} | {' | '.join(vals)} |\n"
        out += "\n"

        # Scaling behavior
        behavior = scaling.get("scaling_behavior", {})
        if behavior:
            out += "## Scaling Behavior (K8s 1.18+)\n\n"
            out += "```yaml\n"
            out += "behavior:\n"
            out += "  scaleUp:\n"
            out += f"    stabilizationWindowSeconds: {behavior.get('scale_up_stabilization_window_seconds', 0)}\n"
            out += f"    selectPolicy: {behavior.get('scale_up_select_policy', 'Max')}\n"
            policies = behavior.get("scale_up_policies", [])
            if policies:
                out += "    policies:\n"
                for p in policies:
                    out += f"      - type: {p.get('type', 'Percent')}\n"
                    out += f"        value: {p.get('value', 100)}\n"
                    out += f"        periodSeconds: {p.get('periodSeconds', 60)}\n"
            out += "  scaleDown:\n"
            out += f"    stabilizationWindowSeconds: {behavior.get('scale_down_stabilization_window_seconds', 300)}\n"
            out += f"    selectPolicy: {behavior.get('scale_down_select_policy', 'Max')}\n"
            policies = behavior.get("scale_down_policies", [])
            if policies:
                out += "    policies:\n"
                for p in policies:
                    out += f"      - type: {p.get('type', 'Percent')}\n"
                    out += f"        value: {p.get('value', 10)}\n"
                    out += f"        periodSeconds: {p.get('periodSeconds', 60)}\n"
            out += "```\n\n"

        # Target info
        target_kind = scaling.get("target_kind", "Deployment")
        selector_labels = scaling.get("selector_labels", {})
        if selector_labels:
            out += "## Scale Target\n\n"
            out += f"- **Target Kind**: `{target_kind}`\n"
            out += "- **Selector Labels**:\n"
            for k, v in selector_labels.items():
                out += f"  - `{k}: {v}`\n"
            out += "\n"

    # -- Resource estimation details --
    if resources:
        out += "## Resource Estimation Details\n\n"

        # Framework considerations
        fw = resources.get("framework_considerations", {})
        if fw:
            out += "### Framework-Specific Considerations\n\n"
            out += f"- **Startup Overhead**: {fw.get('startup_overhead_mb', 'N/A')} MB\n"
            out += f"- **Runtime Overhead**: {fw.get('runtime_overhead_mb', 'N/A')} MB\n"
            out += f"- **Concurrent Request Impact**: {fw.get('concurrent_request_impact', 'N/A')}\n"
            gc_impact = fw.get("garbage_collection_impact")
            if gc_impact:
                out += f"- **GC Impact**: {gc_impact}\n"
            heap = fw.get("recommended_heap_size")
            if heap:
                out += f"- **Recommended Heap Size**: `{heap}`\n"
            out += "\n"

        # Metadata
        meta = resources.get("metadata", {})
        if meta:
            out += "### Estimation Metadata\n\n"
            out += f"- **Methodology**: {meta.get('estimation_methodology', 'N/A')}\n"
            out += f"- **Confidence Level**: `{meta.get('confidence_level', 'N/A')}`\n\n"

            assumptions = meta.get("assumptions", [])
            if assumptions:
                out += "**Assumptions:**\n"
                for a in assumptions:
                    out += f"- {a}\n"
                out += "\n"

            risks = meta.get("risk_factors", [])
            if risks:
                out += "**Risk Factors:**\n"
                for r in risks:
                    out += f"- ⚠️ {r}\n"
                out += "\n"

            monitoring = meta.get("monitoring_recommendations", [])
            if monitoring:
                out += "### Recommended Monitoring Metrics\n\n"
                out += "Configure these Prometheus metrics for post-deployment observability:\n\n"
                for m in monitoring:
                    out += f"- `{m}`\n"
                out += "\n"

    return out


# ===========================================================================
# 5. Dependencies Blueprint — Helm deps, init containers, sidecars, hooks
# ===========================================================================

def render_dependencies_blueprint(
    deps: Optional[Dict[str, Any]],
) -> str:
    """Render the complete dependencies blueprint."""
    out = "# Dependencies and Lifecycle Hooks\n\n"
    out += "This reference defines all external dependencies, startup ordering,\n"
    out += "sidecar containers, and Helm lifecycle hooks.\n\n"

    if not deps:
        out += "> No dependencies declared by the planner.\n"
        return out

    # -- Helm chart dependencies --
    helm_deps = deps.get("helm_dependencies", [])
    if helm_deps:
        out += "## Helm Chart Dependencies (Subcharts)\n\n"
        out += "Add these to `Chart.yaml` under `dependencies:`\n\n"
        out += "| Name | Repository | Version | Condition | Reason |\n"
        out += "|---|---|---|---|---|\n"
        for d in helm_deps:
            out += (
                f"| {d.get('name', 'N/A')} "
                f"| {d.get('repository', 'N/A')} "
                f"| {d.get('version', '*')} "
                f"| `{d.get('condition', 'N/A')}` "
                f"| {d.get('reason', 'N/A')} |\n"
            )
        out += "\n"

        # Chart.yaml snippet
        out += "### Chart.yaml Dependencies Block\n\n"
        out += "```yaml\ndependencies:\n"
        for d in helm_deps:
            out += f"  - name: {d.get('name')}\n"
            out += f"    version: \"{d.get('version', '*')}\"\n"
            repo = d.get("repository")
            if repo:
                out += f"    repository: {repo}\n"
            cond = d.get("condition")
            if cond:
                out += f"    condition: {cond}\n"
            alias = d.get("alias")
            if alias:
                out += f"    alias: {alias}\n"
        out += "```\n\n"
    else:
        out += "## Helm Chart Dependencies\n\n> No subchart dependencies.\n\n"

    # -- Init containers --
    init_containers = deps.get("init_containers_needed", [])
    if init_containers:
        out += "## Init Containers\n\n"
        out += "These run sequentially before the main container starts:\n\n"
        for i, ic in enumerate(init_containers, 1):
            out += f"### {i}. `{ic.get('name', 'init')}`\n\n"
            out += f"- **Image**: `{ic.get('image', 'busybox:latest')}`\n"
            out += f"- **Purpose**: {ic.get('purpose', 'N/A')}\n"
            dur = ic.get("estimated_duration_seconds")
            if dur:
                out += f"- **Estimated Duration**: ~{dur}s\n"
            retry = ic.get("retry_policy", "Never")
            out += f"- **Restart Policy**: `{retry}`\n\n"

        # Deployment template snippet
        out += "### Init Containers in Deployment Template\n\n"
        out += "```yaml\n"
        out += "initContainers:\n"
        for ic in init_containers:
            name = ic.get("name", "init")
            image = ic.get("image", "busybox:latest")
            out += f"  - name: {name}\n"
            out += f"    image: {image}\n"
            if name.startswith("wait-for"):
                out += "    command:\n"
                out += "      - 'sh'\n"
                out += "      - '-c'\n"
                out += "      - 'until pg_isready -h $DB_HOST -p $DB_PORT; do echo waiting; sleep 2; done'\n"
            elif "migrate" in name:
                out += "    command:\n"
                out += "      - 'sh'\n"
                out += "      - '-c'\n"
                out += "      - 'python manage.py migrate --noinput || alembic upgrade head'\n"
            out += "    envFrom:\n"
            out += "      - configMapRef:\n"
            out += "          name: {{ include \"CHARTNAME.fullname\" . }}-config\n"
            out += "      - secretRef:\n"
            out += "          name: {{ include \"CHARTNAME.fullname\" . }}-secret\n"
        out += "```\n\n"
    else:
        out += "## Init Containers\n\n> No init containers required.\n\n"

    # -- Sidecars --
    sidecars = deps.get("sidecars_needed", [])
    if sidecars:
        out += "## Sidecar Containers\n\n"
        out += "These run alongside the main container:\n\n"
        for sc in sidecars:
            out += f"### `{sc.get('name', 'sidecar')}`\n\n"
            out += f"- **Image**: `{sc.get('image', 'N/A')}`\n"
            out += f"- **Purpose**: {sc.get('purpose', 'N/A')}\n"
            comm = sc.get("communication_type")
            if comm:
                out += f"- **Communication**: `{comm}`\n"
            impact = sc.get("resource_impact")
            if impact:
                out += f"- **Resource Impact**: `{impact}`\n"
            out += "\n"
    else:
        out += "## Sidecar Containers\n\n> No sidecar containers required.\n\n"

    # -- Helm hooks --
    helm_hooks = deps.get("helm_hooks", [])
    if helm_hooks:
        out += "## Helm Lifecycle Hooks\n\n"
        out += "| Hook Type | Name | Purpose | Weight | Delete Policy |\n"
        out += "|---|---|---|---|---|\n"
        for h in helm_hooks:
            dp = h.get("delete_policy", [])
            dp_str = ", ".join(dp) if isinstance(dp, list) else str(dp)
            out += (
                f"| `{h.get('hook_type', 'N/A')}` "
                f"| `{h.get('name', 'N/A')}` "
                f"| {h.get('purpose', 'N/A')} "
                f"| {h.get('weight', 0)} "
                f"| {dp_str} |\n"
            )
        out += "\n"

        # Hook template snippet
        out += "### Hook Job Template\n\n"
        out += "```yaml\n"
        for h in helm_hooks:
            hook_type = h.get("hook_type", "post-install")
            hook_name = h.get("name", "hook")
            weight = h.get("weight", 0)
            dp = h.get("delete_policy", ["before-hook-creation"])
            dp_str = ",".join(dp) if isinstance(dp, list) else str(dp)
            out += f"apiVersion: batch/v1\n"
            out += f"kind: Job\n"
            out += f"metadata:\n"
            out += f"  name: {{{{ include \"CHARTNAME.fullname\" . }}}}-{hook_name}\n"
            out += f"  annotations:\n"
            out += f"    \"helm.sh/hook\": {hook_type}\n"
            out += f"    \"helm.sh/hook-weight\": \"{weight}\"\n"
            out += f"    \"helm.sh/hook-delete-policy\": {dp_str}\n"
            out += f"spec:\n"
            out += f"  template:\n"
            out += f"    spec:\n"
            out += f"      restartPolicy: Never\n"
            out += f"      containers:\n"
            out += f"        - name: {hook_name}\n"
            out += f"          image: {{{{ .Values.image.repository }}}}:{{{{ .Values.image.tag }}}}\n"
            hook_purpose = h.get("purpose", "run hook")
            cmd_line = "          command: ['sh', '-c', '# " + hook_purpose + "']\n"
            out += cmd_line
            out += "---\n"
        out += "```\n\n"
    else:
        out += "## Helm Lifecycle Hooks\n\n> No hooks defined.\n\n"

    # -- Dependency rationale --
    rationale = deps.get("dependency_rationale", "")
    if rationale:
        out += "## Dependency Rationale\n\n"
        out += f"> {rationale}\n\n"

    # -- Warnings --
    warnings = deps.get("warnings", [])
    if warnings:
        out += "## ⚠️ Dependency Warnings\n\n"
        for w in warnings:
            out += f"- {w}\n"
        out += "\n"

    return out


# ===========================================================================
# 6. Security Blueprint — SecurityContext, NetworkPolicy, RBAC
# ===========================================================================

def render_security_blueprint(
    application_analysis: Optional[Dict[str, Any]],
    kubernetes_architecture: Optional[Dict[str, Any]],
) -> str:
    """Render security hardening blueprint."""
    out = "# Security Blueprint\n\n"
    out += "This reference defines all security configurations the Helm chart\n"
    out += "must implement to meet production hardening standards.\n\n"

    security = {}
    if application_analysis:
        security = application_analysis.get("security", {})

    # -- Pod Security Context --
    out += "## Pod Security Context\n\n"
    out += "```yaml\n"
    out += "securityContext:\n"
    out += f"  runAsNonRoot: {str(security.get('run_as_non_root', True)).lower()}\n"
    out += "  runAsUser: 1000\n"
    out += "  runAsGroup: 1000\n"
    out += f"  readOnlyRootFilesystem: {str(security.get('read_only_root_filesystem', False)).lower()}\n"
    out += "  allowPrivilegeEscalation: false\n"
    out += "  capabilities:\n"
    out += "    drop:\n"
    for cap in security.get("capabilities_to_drop", ["ALL"]):
        out += f"      - {cap}\n"
    out += "```\n\n"

    # -- NetworkPolicy --
    np_resource = None
    if kubernetes_architecture:
        aux = _get(kubernetes_architecture, "resources", "auxiliary", default=[])
        for r in aux:
            if r.get("type") == "NetworkPolicy":
                np_resource = r
                break

    out += "## NetworkPolicy Configuration\n\n"
    if np_resource:
        hints = np_resource.get("configuration_hints", {})
        tradeoffs = np_resource.get("tradeoffs", "")
        out += "The chart should include a `networkpolicy.yaml` template guarded by\n"
        out += "`.Values.networkPolicy.enabled`.\n\n"

        pod_selector = hints.get("podSelector", {})
        policy_types = hints.get("policyTypes", [])
        ingress_rule = hints.get("ingress", "")

        out += "**Design:**\n\n"
        if pod_selector:
            labels = pod_selector.get("matchLabels", {})
            out += "- **Pod Selector**: "
            out += ", ".join(f"`{k}: {v}`" for k, v in labels.items())
            out += "\n"
        if policy_types:
            out += f"- **Policy Types**: {', '.join(f'`{t}`' for t in policy_types)}\n"
        if ingress_rule:
            out += f"- **Ingress Rule**: {ingress_rule}\n"
        if tradeoffs:
            out += f"\n> ⚠️ {tradeoffs}\n"
        out += "\n"

        justification = hints.get("justification", "")
        if justification:
            out += f"> **Rationale**: {justification}\n\n"
    else:
        out += "> No NetworkPolicy specified by the architecture planner.\n\n"

    # -- ServiceAccount --
    sa_resource = None
    if kubernetes_architecture:
        aux = _get(kubernetes_architecture, "resources", "auxiliary", default=[])
        for r in aux:
            if r.get("type") == "ServiceAccount":
                sa_resource = r
                break

    out += "## ServiceAccount Configuration\n\n"
    if sa_resource:
        hints = sa_resource.get("configuration_hints", {})
        tradeoffs = sa_resource.get("tradeoffs", "")
        automount = hints.get("automountServiceAccountToken", False)
        out += f"- **Create dedicated ServiceAccount**: Yes\n"
        out += f"- **automountServiceAccountToken**: `{str(automount).lower()}`\n"
        if tradeoffs:
            out += f"\n> ⚠️ {tradeoffs}\n"
        justification = hints.get("justification", "")
        if justification:
            out += f"\n> **Rationale**: {justification}\n"
    else:
        out += "> Use default ServiceAccount configuration.\n"

    out += "\n"

    # -- Bitnami Security Checklist --
    out += "## Bitnami Security Compliance Checklist\n\n"
    out += "- [ ] Container runs as non-root user (UID 1000)\n"
    out += "- [ ] All Linux capabilities dropped (`drop: [ALL]`)\n"
    out += "- [ ] Privilege escalation disabled\n"
    out += "- [ ] Dedicated ServiceAccount with `automountServiceAccountToken: false`\n"
    out += "- [ ] NetworkPolicy restricts ingress to known sources only\n"
    out += "- [ ] Secrets managed via `Secret` resources (not hardcoded)\n"
    out += "- [ ] Resource limits set to prevent unbounded growth\n"
    out += "- [ ] Health probes verify application readiness\n"

    return out


# ===========================================================================
# 7. Manifest Patterns — expected rendered YAML per resource
# ===========================================================================

def render_manifest_patterns(
    app_name: str,
    kubernetes_architecture: Optional[Dict[str, Any]],
    application_analysis: Optional[Dict[str, Any]],
    dependencies: Optional[Dict[str, Any]] = None,
) -> str:
    """Render concrete expected YAML output for each resource type.

    These are DATA-LEVEL examples (actual rendered YAML), not Go templates.
    The helm-generator uses these as reference targets to understand what the
    final rendered output should look like.
    """
    app_slug = _slugify(app_name)
    out = f"# {app_name.upper()} — Expected Manifest Patterns\n\n"
    out += "These are concrete YAML examples showing the expected **rendered** output\n"
    out += "for each Kubernetes resource. Use these as reference targets when writing\n"
    out += "Helm Go templates. The Go templates themselves should follow patterns from\n"
    out += "the generic `helm-generator` skill references.\n\n"

    if not kubernetes_architecture:
        out += "> No architecture data available.\n"
        return out

    k8s_res = kubernetes_architecture.get("resources", {})
    core_resources = k8s_res.get("core", [])
    aux_resources = k8s_res.get("auxiliary", [])

    # Extract framework details for probes
    framework = {}
    security = {}
    networking = {}
    if application_analysis:
        framework = application_analysis.get("framework_analysis", {})
        security = application_analysis.get("security", {})
        networking = application_analysis.get("networking", {})

    container_port = networking.get("port", 8080)

    # -- Deployment example --
    deployment = next(
        (cr for cr in core_resources if cr.get("type") == "Deployment"), None
    )
    if deployment:
        kcp = deployment.get("key_configuration_parameters", {})
        image = kcp.get("image", f"{app_slug}:latest")
        replicas = kcp.get("replicas", 1)
        liveness = kcp.get("liveness_probe", "/healthz")
        readiness = kcp.get("readiness_probe", "/readyz")
        startup_delay = kcp.get("startup_probe_initial_delay", 15)
        shutdown = kcp.get("graceful_shutdown_period", 30)
        sec_ctx = kcp.get("security_context", {})

        out += f"## Deployment\n\n"
        out += "```yaml\n"
        out += f"apiVersion: apps/v1\n"
        out += f"kind: Deployment\n"
        out += f"metadata:\n"
        out += f"  name: {app_slug}\n"
        out += f"  labels:\n"
        out += f"    app.kubernetes.io/name: {app_slug}\n"
        out += f"spec:\n"
        out += f"  replicas: {replicas}\n"
        out += f"  selector:\n"
        out += f"    matchLabels:\n"
        out += f"      app.kubernetes.io/name: {app_slug}\n"
        out += f"  template:\n"
        out += f"    metadata:\n"
        out += f"      labels:\n"
        out += f"        app.kubernetes.io/name: {app_slug}\n"
        out += f"    spec:\n"
        out += f"      terminationGracePeriodSeconds: {shutdown}\n"
        out += f"      serviceAccountName: {app_slug}\n"
        out += f"      securityContext:\n"
        out += f"        fsGroup: 1000\n"

        # Init containers
        if dependencies:
            init_containers = dependencies.get("init_containers_needed", [])
            if init_containers:
                out += f"      initContainers:\n"
                for ic in init_containers:
                    out += f"        - name: {ic.get('name', 'init')}\n"
                    out += f"          image: {ic.get('image', 'busybox:latest')}\n"
                    out += f"          # Purpose: {ic.get('purpose', 'N/A')}\n"

        out += f"      containers:\n"
        out += f"        - name: {app_slug}\n"
        out += f"          image: {image}\n"
        out += f"          imagePullPolicy: IfNotPresent\n"
        out += f"          ports:\n"
        out += f"            - name: http\n"
        out += f"              containerPort: {container_port}\n"
        out += f"              protocol: TCP\n"
        out += f"          securityContext:\n"
        out += f"            runAsNonRoot: {str(sec_ctx.get('run_as_non_root', True)).lower()}\n"
        out += f"            allowPrivilegeEscalation: false\n"
        out += f"            capabilities:\n"
        out += f"              drop:\n"
        for cap in sec_ctx.get("drop_capabilities", ["ALL"]):
            out += f"                - {cap}\n"
        out += f"          livenessProbe:\n"
        out += f"            httpGet:\n"
        out += f"              path: {liveness}\n"
        out += f"              port: http\n"
        out += f"            initialDelaySeconds: {startup_delay}\n"
        out += f"          readinessProbe:\n"
        out += f"            httpGet:\n"
        out += f"              path: {readiness}\n"
        out += f"              port: http\n"
        out += f"            initialDelaySeconds: {max(5, startup_delay - 10)}\n"
        out += f"          resources:\n"
        out += f"            requests:\n"
        out += f"              cpu: 250m\n"
        out += f"              memory: 256Mi\n"
        out += f"            limits:\n"
        out += f"              cpu: 500m\n"
        out += f"              memory: 512Mi\n"

        # Sidecars
        if dependencies:
            sidecars = dependencies.get("sidecars_needed", [])
            for sc in sidecars:
                out += f"        - name: {sc.get('name', 'sidecar')}\n"
                out += f"          image: {sc.get('image', 'N/A')}\n"
                out += f"          # Purpose: {sc.get('purpose', 'N/A')}\n"

        out += "```\n\n"

    # -- Service example --
    svc = next((r for r in aux_resources if r.get("type") == "Service"), None)
    if svc:
        hints = svc.get("configuration_hints", {})
        out += "## Service\n\n"
        out += "```yaml\n"
        out += "apiVersion: v1\n"
        out += "kind: Service\n"
        out += "metadata:\n"
        out += f"  name: {app_slug}\n"
        out += "spec:\n"
        out += f"  type: {hints.get('type', 'ClusterIP')}\n"
        out += "  ports:\n"
        out += f"    - port: {hints.get('port', 80)}\n"
        out += f"      targetPort: {hints.get('target_port', container_port)}\n"
        out += "      protocol: TCP\n"
        out += "      name: http\n"
        out += "  selector:\n"
        out += f"    app.kubernetes.io/name: {app_slug}\n"
        out += "```\n\n"

    # -- Ingress example --
    ingress = next((r for r in aux_resources if r.get("type") == "Ingress"), None)
    if ingress:
        hints = ingress.get("configuration_hints", {})
        hostname = hints.get("hostname", "app.example.com")
        tls_enabled = hints.get("tls_enabled", False)
        annotations = hints.get("annotations", {})

        out += "## IngressRoute (Traefik)\n\n"
        out += "```yaml\n"
        out += "apiVersion: traefik.io/v1alpha1\n"
        out += "kind: IngressRoute\n"
        out += "metadata:\n"
        out += f"  name: {app_slug}\n"
        if annotations:
            out += "  annotations:\n"
            for k, v in annotations.items():
                out += f"    {k}: \"{v}\"\n"
        out += "spec:\n"
        out += "  entryPoints:\n"
        out += "    - web\n"
        if tls_enabled:
            out += "    - websecure\n"
        out += "  routes:\n"
        out += f"    - match: Host(`{hostname}`)\n"
        out += "      kind: Rule\n"
        out += "      services:\n"
        out += f"        - name: {app_slug}\n"
        out += "          port: 80\n"
        if tls_enabled:
            out += "  tls:\n"
            out += f"    secretName: {_slugify(hostname)}-tls\n"
        out += "```\n\n"

    return out
